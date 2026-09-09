"""End-to-end stdio lifecycle for the PRODUCTION bot ACP stack.

Spawns a real stdio subprocess (SDK 0.12.1 ``acp.spawn_agent_process``)
running ``tests/acp_stdio_agent.py`` — the production ``AcpRuntime`` +
``BotService`` initialization (pool assembly from a scope declaration, ACP
input pipeline, approval runtime, pool request admission) over the framework
entry (``run_acp_entry`` → ``modex_agent.acp.entry.main``). The only test seam lives inside the
subprocess: the module-level
``bot.service.model_provider.create_llm_provider`` binding is monkeypatched
to return a scripted provider — the single factory seam every real provider
construction already routes through. The temp config carries a REAL
``model.yml`` (without it BotService boots the ``_unconfigured`` placeholder
and the factory seam is never reached). No network, no production changes.

Covered:
- initialize + new_session with the protocol cwd bound to the project;
- a normal prompt: message chunk + ``write`` tool card + tool result, with a
  RELATIVE path proving the workspace-scoped tool resolves against the bound
  project root (not the subprocess cwd);
- permission approve (``allow_once``) -> tool executes, turn ends;
- graceful client exit: explicit ``conn.close()`` (EOF) -> the agent drains
  (BotService.stop) and exits rc 0 within a RAISED SDK shutdown_timeout
  (the SDK default 2s would terminate before the drain finishes);
- restart + ``load_session`` replay of persisted history, then a fresh
  prompt on the loaded session;
- mid-approval ``session/cancel``: the pending permission turn ends
  ``cancelled``, the blocked tool never ran, and a follow-up prompt in the
  SAME session completes normally without re-running the old tool;
- backend parameterization: the same lifecycle runs under BOTH persistence
  backends (FILE and SQLITE). Each variant verifies its OWN data root — the
  workspace ``state.db`` exists only under sqlite; file must not create one —
  and that nothing is written outside the data root. ``initialize`` alone
  must not touch the project directory, and the restart/load follow-up turn
  adds no new top-level entries (project root or data root). Replay carries
  the original prompt text verbatim (source consistency);
- IM-only control commands (/cd, /pool, /exit) are NOT provider-dispatched
  over ACP: each terminates at UnsupportedCommandStage with the generic
  unknown-command notice — no provider ack, no tool call, no file write.

Every spawn/RPC/shutdown step is bounded with ``asyncio.wait_for`` so a hang
fails the test instead of the suite. The subprocess stderr pipe is drained
by a background reader task for the agent's whole lifetime (the SDK pipes
stderr but never reads it — an undrained pipe can block the agent once the
OS buffer fills); on any failure the collected stderr is attached VERBATIM
to the original exception via ``add_note`` (exception type/traceback are
never replaced), and a non-zero exit after teardown fails inside the
context manager with the full stderr included.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import acp
import pytest
from acp.schema import (
    AgentMessageChunk,
    AllowedOutcome,
    PermissionOption,
    RequestPermissionResponse,
    ToolCallProgress,
    ToolCallStart,
    UserMessageChunk,
)

_TESTS_DIR = Path(__file__).resolve().parent  # .../bot_project/tests/acp
_AGENT_SCRIPT = _TESTS_DIR / "acp_stdio_agent.py"
_REPO_ROOT = _TESTS_DIR.parents[2]  # acp -> tests -> bot_project -> repo root

_BOOT_TIMEOUT = 30.0
_PROMPT_TIMEOUT = 20.0
_SHUTDOWN_GRACE_SECONDS = 10.0


def _agent_env() -> dict[str, str]:
    """Full environment + PYTHONPATH (repo src + bot_project).

    The SDK merges ``env`` over a TRIMMED base (a 12-variable whitelist —
    missing e.g. ``COMSPEC``/``WINDIR``/``ALLUSERSPROFILE``), so we pass the
    complete parent environment like ``tests/acp/test_e2e_turn.py`` does;
    PYTHONPATH deliberately does NOT include ``tests/`` (its ``bot/`` shadow
    package would poison ``import bot`` in the subprocess — the script's own
    directory, ``tests/acp/``, lands at ``sys.path[0]`` regardless).
    """
    env = dict(os.environ)
    pythonpath = os.pathsep.join(
        [str(_REPO_ROOT / "src"), str(_REPO_ROOT / "examples" / "bot_project")]
    )
    if env.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = pythonpath
    return env


class RecordingClient:
    """Minimal SDK client: records session updates, auto-allows permissions."""

    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.permission_requests: list[tuple[Any, list[PermissionOption]]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append(update)

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((tool_call, options))
        return self._allow(options)

    @staticmethod
    def _allow(options: list[PermissionOption]) -> RequestPermissionResponse:
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=options[0].option_id)
        )


class RejectingClient(RecordingClient):
    """Answers the LAST option (``reject_once``) instead of the first."""

    @staticmethod
    def _allow(options: list[PermissionOption]) -> RequestPermissionResponse:
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=options[-1].option_id)
        )


class GatedClient(RecordingClient):
    """Holds each permission request until released (for the cancel test)."""

    def __init__(self) -> None:
        super().__init__()
        self.permission_started = asyncio.Event()
        self.release = asyncio.Event()

    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self.permission_requests.append((tool_call, options))
        self.permission_started.set()
        await self.release.wait()
        return self._allow(options)


@asynccontextmanager
async def _agent_process(client: RecordingClient, settings: Path) -> AsyncIterator[Any]:
    """Bounded spawn/teardown + stderr capture around the SDK's async CM.

    ``shutdown_timeout`` is raised from the SDK default (2s) so the graceful
    window covers the production ``BotService.stop()`` drain; teardown is
    explicitly bounded so a stuck child fails the test instead of hanging it.

    The SDK pipes the agent's stderr but never reads it; a background reader
    task drains it for the whole lifetime (an undrained pipe would block the
    agent once the OS buffer fills) and the collected text (never truncated)
    is attached to any failure via ``add_note`` — the original exception is
    always re-raised unchanged.
    """
    cm = acp.spawn_agent_process(
        client,
        sys.executable,
        str(_AGENT_SCRIPT),
        str(settings),
        env=_agent_env(),
        transport_kwargs={"shutdown_timeout": _SHUTDOWN_GRACE_SECONDS},
    )
    conn, proc = await asyncio.wait_for(cm.__aenter__(), timeout=_BOOT_TIMEOUT)
    stderr_lines: list[bytes] = []

    async def _drain_stderr() -> None:
        # proc.stderr is an asyncio.StreamReader fed by the SDK's PIPE
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_lines.append(line)

    stderr_task = asyncio.create_task(_drain_stderr())

    def _stderr_text() -> str:
        return b"".join(stderr_lines).decode(errors="replace")

    def _note_stderr(exc: BaseException) -> None:
        text = _stderr_text()
        if text:
            exc.add_note(f"--- agent stderr (verbatim) ---\n{text}")

    body_failed = False
    try:
        yield conn, proc
    except BaseException as exc:
        body_failed = True
        _note_stderr(exc)
        raise
    finally:
        # deterministic EOF first (idempotent with the SDK's own close), then
        # the SDK teardown: write_eof -> wait(exit) -> terminate -> kill
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.close(), timeout=_SHUTDOWN_GRACE_SECONDS)
        try:
            await asyncio.wait_for(
                cm.__aexit__(None, None, None),
                timeout=_BOOT_TIMEOUT + _SHUTDOWN_GRACE_SECONDS,
            )
        except BaseException as exc:
            _note_stderr(exc)
            raise
        # the stderr pipe closes when the process exits; drain must finish
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stderr_task, timeout=_BOOT_TIMEOUT)
        if proc.returncode != 0 and not body_failed:
            raise AssertionError(
                f"agent process exited rc={proc.returncode}\n"
                f"--- agent stderr (verbatim) ---\n{_stderr_text()}"
            )


def _write_project_config(
    settings: Path, project: Path, backend: str = "sqlite"
) -> None:
    """Settings dir + editor project + a REAL model.yml.

    Without model.yml BotService boots the ``_unconfigured`` placeholder and
    ``BotModelProvider.chat_stream`` fails fast BEFORE constructing any
    provider — the factory seam under test would never run. The provider
    entry below is never dialed (the factory is monkeypatched in the
    subprocess); it only has to pass ``BotModelConfig`` validation.
    """
    (settings / "scopes").mkdir(parents=True)
    (settings / "bot_config.yml").write_text(
        f"persistence:\n  backend: {backend}\n", encoding="utf-8"
    )
    (settings / "model.yml").write_text(
        "default_provider: scripted\n"
        "default_model: scripted-model\n"
        "providers:\n"
        "  - key: scripted\n"
        "    name: scripted\n"
        "    base_url: http://127.0.0.1:1\n"
        "    api_key: test-not-dialed\n"
        "    models:\n"
        "      - name: scripted-model\n"
        "        model: scripted-acp\n",
        encoding="utf-8",
    )
    (settings / "scopes" / "bot.yml").write_text(
        "pool:\n"
        "  name: main\n"
        "  agents:\n"
        "    main:\n"
        "      use_terminal: false\n"
        "      capabilities:\n"
        "        skills: false\n"
        "      approval:\n"
        "        enabled: true\n"
        "        tools:\n"
        "          write:\n"
        "            allowed_paths: []\n",
        encoding="utf-8",
    )
    project.mkdir(parents=True, exist_ok=True)


def _project_entry_set(project: Path) -> set[str]:
    """Top-level project entries EXCLUDING the data root (checked separately
    per backend)."""
    return {p.name for p in project.iterdir()} - {".modex"}


@pytest.mark.parametrize("backend", ["file", "sqlite"])
async def test_stdio_lifecycle_prompt_approve_restart_load(
    tmp_path: Path, backend: str
) -> None:
    settings = tmp_path / "settings"
    project = tmp_path / "editor-project"
    _write_project_config(settings, project, backend=backend)
    data_root = project / ".modex"

    client = RecordingClient()
    async with _agent_process(client, settings) as (conn, proc):
        # initialize alone must not write into the project directory —
        # binding happens at new_session, not before (the config writer
        # pre-created the empty project dir; it must still be empty)
        init = await asyncio.wait_for(
            conn.initialize(protocol_version=acp.PROTOCOL_VERSION), _BOOT_TIMEOUT
        )
        assert init.protocol_version == 1
        assert list(project.iterdir()) == []
        # 1. new_session binds the protocol cwd to the project
        session = await asyncio.wait_for(
            conn.new_session(cwd=str(project)), timeout=_BOOT_TIMEOUT
        )
        assert session.session_id
        assert data_root.is_dir()
        # The inbox uses SQLite for both backends; transcript artifacts below
        # distinguish the selected conversation persistence backend.
        assert (data_root / "state.db").exists()
        project_entries_before_turn = _project_entry_set(project)

        # 2. normal prompt: relative path — the workspace-scoped write tool
        #    must resolve it against the BOUND PROJECT root, never the
        #    subprocess cwd
        response = await asyncio.wait_for(
            conn.prompt(
                session_id=session.session_id,
                prompt=[acp.text_block("WRITE relnote.txt hello-editor")],
            ),
            timeout=_PROMPT_TIMEOUT,
        )
        assert response.stop_reason == "end_turn"
        assert (project / "relnote.txt").read_text(encoding="utf-8") == "hello-editor"
        messages = [u for u in client.updates if isinstance(u, AgentMessageChunk)]
        assert any("tool-finished" in (m.content.text or "") for m in messages)
        starts = [u for u in client.updates if isinstance(u, ToolCallStart)]
        assert any(s.title == "write" for s in starts)
        progress = [u for u in client.updates if isinstance(u, ToolCallProgress)]
        assert any(p.status == "completed" for p in progress)

        # 3. permission approve path: approval-enabled pool escalates write
        #    (step 2's write was escalated too and auto-allowed — clear both)
        client.updates.clear()
        client.permission_requests.clear()
        response = await asyncio.wait_for(
            conn.prompt(
                session_id=session.session_id,
                prompt=[acp.text_block("WRITE secret.txt needs-approval")],
            ),
            timeout=_PROMPT_TIMEOUT,
        )
        assert response.stop_reason == "end_turn"
        assert len(client.permission_requests) == 1
        _tool_call, options = client.permission_requests[0]
        assert [o.option_id for o in options] == ["allow_once", "reject_once"]
        assert (project / "secret.txt").read_text(encoding="utf-8") == "needs-approval"
        session_id = session.session_id

    # 4. EOF teardown was graceful: no terminate, clean exit code
    assert proc.returncode == 0
    # extra-write check: turn 1+2 only added the two intended files at the
    # project top level; the data root grew (persistence) but stayed inside
    # itself — nothing outside the data root and the two files was touched
    assert _project_entry_set(project) - project_entries_before_turn == {
        "relnote.txt",
        "secret.txt",
    }
    if backend == "file":
        assert list((data_root / "sessions" / "main").glob("*.jsonl"))

    # 5. restart: load_session replays persisted history, then a fresh prompt
    reloaded_client = RecordingClient()
    async with _agent_process(reloaded_client, settings) as (conn2, proc2):
        loaded = await asyncio.wait_for(
            conn2.load_session(cwd=str(project), session_id=session_id),
            timeout=_BOOT_TIMEOUT,
        )
        assert loaded is not None
        replay_users = [
            u
            for u in reloaded_client.updates
            if isinstance(u, UserMessageChunk) and u.content.text
        ]
        assert any("WRITE" in (u.content.text or "") for u in replay_users)
        # source consistency: the persisted history replays the ORIGINAL
        # prompt text verbatim, not a rebuilt approximation
        assert any(
            (u.content.text or "").startswith("WRITE relnote.txt hello-editor")
            for u in replay_users
        )
        replay_starts = [
            u for u in reloaded_client.updates if isinstance(u, ToolCallStart)
        ]
        assert any(s.title == "write" for s in replay_starts)

        entries_before_reload_turn = _project_entry_set(project)
        response = await asyncio.wait_for(
            conn2.prompt(
                session_id=session_id, prompt=[acp.text_block("hello again")]
            ),
            timeout=_PROMPT_TIMEOUT,
        )
        assert response.stop_reason == "end_turn"
        messages = [u for u in reloaded_client.updates if isinstance(u, AgentMessageChunk)]
        assert any("ack: hello again" in (m.content.text or "") for m in messages)
        # the follow-up turn wrote NOTHING new at the project top level (the
        # data root may grow — that is its job)
        assert _project_entry_set(project) == entries_before_reload_turn
    assert proc2.returncode == 0


async def test_stdio_cancel_pending_approval_then_next_prompt(tmp_path: Path) -> None:
    """Mid-approval cancel: turn ends cancelled, tool never runs, the next
    prompt in the SAME session completes normally without re-running it."""
    settings = tmp_path / "settings"
    project = tmp_path / "editor-project"
    _write_project_config(settings, project)

    client = GatedClient()
    async with _agent_process(client, settings) as (conn, proc):
        session = await asyncio.wait_for(
            conn.new_session(cwd=str(project)), timeout=_BOOT_TIMEOUT
        )

        # prompt suspends on the approval card and is HELD there by the client
        prompt_task = asyncio.create_task(
            conn.prompt(
                session_id=session.session_id,
                prompt=[acp.text_block("WRITE never-created.txt blocked-write")],
            )
        )
        await asyncio.wait_for(client.permission_started.wait(), _PROMPT_TIMEOUT)
        await asyncio.wait_for(conn.cancel(session_id=session.session_id), _PROMPT_TIMEOUT)
        response = await asyncio.wait_for(prompt_task, _PROMPT_TIMEOUT)

        assert response.stop_reason == "cancelled"
        assert len(client.permission_requests) == 1
        assert not (project / "never-created.txt").exists()

        # unblock the abandoned client-side permission handler (its response
        # is discarded — the agent-side decision task was already cancelled)
        client.release.set()

        # follow-up prompt on the same session: normal ack, old tool NOT retried
        client.updates.clear()
        response = await asyncio.wait_for(
            conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("hello after cancel")]
            ),
            timeout=_PROMPT_TIMEOUT,
        )
        assert response.stop_reason == "end_turn"
        messages = [u for u in client.updates if isinstance(u, AgentMessageChunk)]
        assert any("ack: hello after cancel" in (m.content.text or "") for m in messages)
        assert not (project / "never-created.txt").exists()
    assert proc.returncode == 0


async def test_stdio_permission_reject_ends_turn_without_effect(tmp_path: Path) -> None:
    """Rejecting the permission card denies the tool; the turn still ends."""
    settings = tmp_path / "settings"
    project = tmp_path / "editor-project"
    _write_project_config(settings, project)

    client = RejectingClient()
    async with _agent_process(client, settings) as (conn, proc):
        session = await asyncio.wait_for(
            conn.new_session(cwd=str(project)), timeout=_BOOT_TIMEOUT
        )
        response = await asyncio.wait_for(
            conn.prompt(
                session_id=session.session_id,
                prompt=[acp.text_block("WRITE never.txt blocked")],
            ),
            timeout=_PROMPT_TIMEOUT,
        )
        assert response.stop_reason == "end_turn"
        assert len(client.permission_requests) == 1
        assert not (project / "never.txt").exists()
    assert proc.returncode == 0


async def test_stdio_im_control_commands_not_provider_dispatched(tmp_path: Path) -> None:
    """/cd, /pool, /exit are IM-only (S2) — the ACP pipeline omits S2, so
    they terminate at UnsupportedCommandStage with the generic notice. No
    provider ack, no permission card, no tool call, no file writes."""
    settings = tmp_path / "settings"
    project = tmp_path / "editor-project"
    _write_project_config(settings, project)

    client = RecordingClient()
    async with _agent_process(client, settings) as (conn, proc):
        await asyncio.wait_for(
            conn.initialize(protocol_version=acp.PROTOCOL_VERSION), _BOOT_TIMEOUT
        )
        session = await asyncio.wait_for(
            conn.new_session(cwd=str(project)), timeout=_BOOT_TIMEOUT
        )
        project_entries_before = _project_entry_set(project)

        for command in ("/cd elsewhere", "/pool other", "/exit"):
            client.updates.clear()
            client.permission_requests.clear()
            response = await asyncio.wait_for(
                conn.prompt(
                    session_id=session.session_id,
                    prompt=[acp.text_block(command)],
                ),
                timeout=_PROMPT_TIMEOUT,
            )
            assert response.stop_reason == "end_turn"
            messages = [
                u for u in client.updates if isinstance(u, AgentMessageChunk)
            ]
            assert len(messages) == 1, command
            command_name = command[1:].split()[0]
            notice = messages[0].content.text or ""
            # exact single generic notice (not provider output)
            assert notice.startswith(f"Unknown command: /{command_name}"), (command, notice)
            assert "No such command or skill is available." in notice, (command, notice)
            # not provider-dispatched: no ack echo, no permission escalation,
            # no tool execution
            assert not any(
                "ack:" in (m.content.text or "") for m in messages
            ), command
            assert client.permission_requests == [], command
            assert not [
                u for u in client.updates if isinstance(u, ToolCallStart)
            ], command
            # no workspace/pool side effects on disk
            assert not (project / "elsewhere").exists()
            assert _project_entry_set(project) == project_entries_before
    assert proc.returncode == 0
