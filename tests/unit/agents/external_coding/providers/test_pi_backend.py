"""Unit tests for ``PiBackend`` (T7).

Every test uses a :class:`FakeProcess` double — no real ``pi`` binary
is ever spawned. The OS-layer functions (``resolve_executable``,
``spawn_process_group``) are monkey-patched so the backend module
calls the test fakes instead of the real subprocess layer.

Coverage shape:

- ABC adherence (:class:`StreamingProviderBackend`).
- ``_build_args`` — flag ordering for every ExecOptions combination.
- ``_session_path`` — resume vs. fresh path derivation.
- Full ``execute_streaming`` — stdout parsing, emission forwarding,
  stdin closing, stderr tail capture, returncode mapping.
- Stale-session detection (stderr-driven + exception-driven).
- ``@pytest.mark.manual`` smoke-test documentation for operators.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from modex_agent.agents.external_coding import (
    Emission,
    ExecOptions,
    ExternalCodingEvent,
)
from modex_agent.agents.external_coding.agent import (
    StaleSessionError,
    StreamingProviderBackend,
)
from modex_agent.agents.external_coding.os_layer import ResolvedExecutable
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.pi_backend import PiBackend

# ---------------------------------------------------------------------------
# Fake subprocess doubles
# ---------------------------------------------------------------------------


class _FakeStdout:
    """Mimics ``asyncio.StreamReader`` for ``async for line in proc.stdout``."""

    def __init__(self, lines: list[str]) -> None:
        self._lines: list[bytes] = [line.encode("utf-8") for line in lines]

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeStderr:
    """Mimics ``asyncio.StreamReader.read()`` for stderr capture."""

    def __init__(self, data: str | bytes = b"") -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data


class FakeProcess:
    """Minimal double for ``asyncio.subprocess.Process``."""

    def __init__(
        self,
        stdout_lines: list[str] | None = None,
        stderr: str | bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStdout(stdout_lines or [])
        self.stderr = _FakeStderr(stderr)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


def _make_collector() -> tuple[list[Emission], Callable[[Emission], Awaitable[None]]]:
    """Return ``(sink, callback)`` — callback appends each emission to sink."""
    sink: list[Emission] = []

    async def _collect(emission: Emission) -> None:
        sink.append(emission)

    return sink, _collect


def _pi_line(payload: dict[str, object]) -> str:
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_os_layer(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch ``resolve_executable`` + ``spawn_process_group`` in pi_backend.

    Returns a state dict. Tests set ``state["process"]`` to a
    :class:`FakeProcess` before calling ``execute_streaming``. The
    dict also captures the spawn call's ``args``, ``cwd``, ``env``,
    and ``stdin`` for post-call assertions.
    """
    state: dict[str, object] = {}

    def fake_resolve(
        name: str,
        logger: object = None,
    ) -> ResolvedExecutable:
        return ResolvedExecutable(argv0=name)

    async def fake_spawn(
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        stdin: int | None,
    ) -> FakeProcess:
        state["args"] = list(args)
        state["cwd"] = cwd
        state["env"] = dict(env)
        state["stdin"] = stdin
        proc = state.get("process")
        if proc is None:
            raise RuntimeError(
                "Test setup error: set state['process'] before calling execute_streaming"
            )
        assert isinstance(proc, FakeProcess)
        return proc

    monkeypatch.setattr(
        "modex_agent.agents.external_coding.providers.pi_backend.resolve_executable",
        fake_resolve,
    )
    monkeypatch.setattr(
        "modex_agent.agents.external_coding.providers.pi_backend.spawn_process_group",
        fake_spawn,
    )
    return state


# ---------------------------------------------------------------------------
# ABC adherence
# ---------------------------------------------------------------------------


class TestPiBackendABC:
    def test_is_streaming_provider_backend(self) -> None:
        assert isinstance(PiBackend(), StreamingProviderBackend)


# ---------------------------------------------------------------------------
# _build_args
# ---------------------------------------------------------------------------


class TestPiBackendBuildArgs:
    """``_build_args`` — flag ordering for every ExecOptions combination."""

    def test_minimal_args(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(prompt="hello", workdir=tmp_path)
        args = backend._build_args(opts, "/tmp/sess.jsonl")
        assert args == [
            "-p",
            "--mode",
            "json",
            "--session",
            "/tmp/sess.jsonl",
            "hello",
        ]

    def test_args_with_model(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(prompt="hello", workdir=tmp_path, model="gpt-4o")
        args = backend._build_args(opts, "/tmp/sess.jsonl")
        assert "--model" in args
        assert args[args.index("--model") + 1] == "gpt-4o"

    def test_args_with_system_prompt(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(
            prompt="hello",
            workdir=tmp_path,
            system_prompt="You are a coder.",
        )
        args = backend._build_args(opts, "/tmp/sess.jsonl")
        assert "--append-system-prompt" in args
        idx = args.index("--append-system-prompt")
        assert args[idx + 1] == "You are a coder."

    def test_args_with_provider_flag(self, tmp_path: Path) -> None:
        backend = PiBackend(provider="anthropic")
        opts = ExecOptions(prompt="hello", workdir=tmp_path)
        args = backend._build_args(opts, "/tmp/sess.jsonl")
        assert "--provider" in args
        idx = args.index("--provider")
        assert args[idx + 1] == "anthropic"

    def test_args_full_combination(self, tmp_path: Path) -> None:
        backend = PiBackend(provider="openai")
        opts = ExecOptions(
            prompt="do the thing",
            workdir=tmp_path,
            model="o1",
            system_prompt="Be helpful.",
        )
        args = backend._build_args(opts, "/data/session.jsonl")
        # Flag order per spec:
        # -p --mode json --session <path> [--provider X --model Y]
        # [--append-system-prompt <s>] <prompt>
        assert args == [
            "-p",
            "--mode",
            "json",
            "--session",
            "/data/session.jsonl",
            "--provider",
            "openai",
            "--model",
            "o1",
            "--append-system-prompt",
            "Be helpful.",
            "do the thing",
        ]

    def test_prompt_is_last_positional(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(
            prompt="FINAL_PROMPT",
            workdir=tmp_path,
            model="m",
            system_prompt="s",
        )
        args = backend._build_args(opts, "/s.jsonl")
        assert args[-1] == "FINAL_PROMPT"


# ---------------------------------------------------------------------------
# _session_path
# ---------------------------------------------------------------------------


class TestPiBackendSessionPath:
    def test_resume_uses_provided_session_id(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(
            prompt="x",
            workdir=tmp_path,
            resume_session_id="/existing/session.jsonl",
        )
        assert backend._session_path(opts) == "/existing/session.jsonl"

    def test_fresh_derives_canonical_path(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        expected = str(ExternalPaths(tmp_path).provider_session(ProviderKind.PI))
        assert backend._session_path(opts) == expected

    def test_fresh_path_matches_external_paths_layout(self, tmp_path: Path) -> None:
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        path = Path(backend._session_path(opts))
        assert path.name == "pi-session.jsonl"
        assert ".modex" in path.parts
        assert "external" in path.parts


# ---------------------------------------------------------------------------
# execute_streaming — full lifecycle
# ---------------------------------------------------------------------------


class TestPiBackendExecute:
    @pytest.mark.asyncio
    async def test_completed_with_emissions(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        lines = [
            _pi_line({"type": "agent_start", "session_id": "psid-1"}),
            _pi_line(
                {
                    "type": "message_update",
                    "update": {"subtype": "text_delta", "delta": "hello"},
                }
            ),
            _pi_line({"type": "turn_end"}),
        ]
        mock_os_layer["process"] = FakeProcess(stdout_lines=lines, returncode=0)
        backend = PiBackend()
        opts = ExecOptions(prompt="hello", workdir=tmp_path)
        sink, callback = _make_collector()

        result = await backend.execute_streaming(opts, {}, callback)

        assert result.status == "completed"
        assert result.error is None
        # Session path derived from workdir.
        assert result.session_id is not None
        assert result.session_id.endswith("pi-session.jsonl")
        # Two emissions: the agent_start/turn_end are no-ops; the
        # message_update yields one TEXT_DELTA.
        assert len(sink) == 1
        assert sink[0].event is ExternalCodingEvent.TEXT_DELTA
        assert sink[0].text == "hello"

    @pytest.mark.asyncio
    async def test_stdin_closed_immediately(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert mock_os_layer["stdin"] == asyncio.subprocess.DEVNULL

    @pytest.mark.asyncio
    async def test_cwd_set_to_workdir(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert mock_os_layer["cwd"] == tmp_path

    @pytest.mark.asyncio
    async def test_env_passed_through(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        env = {"MODEX_TARGETS": "a:b", "PATH": "/usr/bin"}
        await backend.execute_streaming(opts, env, _make_collector()[1])
        assert mock_os_layer["env"] == env

    @pytest.mark.asyncio
    async def test_full_args_includes_resolved_argv0(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = PiBackend(provider="openai")
        opts = ExecOptions(prompt="hi", workdir=tmp_path, model="gpt-4o")
        await backend.execute_streaming(opts, {}, _make_collector()[1])
        args = mock_os_layer["args"]
        assert isinstance(args, list)
        # argv0 is "pi" (from fake resolve_executable).
        assert args[0] == "pi"
        # Followed by Pi-specific flags.
        assert "-p" in args
        assert "--mode" in args

    @pytest.mark.asyncio
    async def test_resume_session_id_used_as_session_path(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(returncode=0)
        backend = PiBackend()
        opts = ExecOptions(
            prompt="x",
            workdir=tmp_path,
            resume_session_id="/prior/session.jsonl",
        )
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.session_id == "/prior/session.jsonl"
        args = mock_os_layer["args"]
        assert isinstance(args, list)
        assert "/prior/session.jsonl" in args

    @pytest.mark.asyncio
    async def test_empty_stdout_completes(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(stdout_lines=[], returncode=0)
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        sink, callback = _make_collector()
        result = await backend.execute_streaming(opts, {}, callback)
        assert result.status == "completed"
        assert sink == []

    @pytest.mark.asyncio
    async def test_nonzero_exit_without_stderr_returns_failed(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(stdout_lines=[], stderr=b"", returncode=1)
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.status == "failed"
        assert result.error is None  # no stderr to report


# ---------------------------------------------------------------------------
# stderr tail capture
# ---------------------------------------------------------------------------


class TestPiBackendStderrCapture:
    @pytest.mark.asyncio
    async def test_stderr_tail_in_error_on_nonzero_exit(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="Error: something went wrong\ntraceback here",
            returncode=2,
        )
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.status == "failed"
        assert result.error is not None
        assert "something went wrong" in result.error

    @pytest.mark.asyncio
    async def test_stderr_tail_truncated_to_limit(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        # Generate a stderr payload larger than the 2000-char limit.
        long_tail = "B" * 5000
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr=long_tail,
            returncode=1,
        )
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.error is not None
        assert len(result.error) <= 2000

    @pytest.mark.asyncio
    async def test_stderr_stripped(self, tmp_path: Path, mock_os_layer: dict[str, object]) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="  real error  \n\n",
            returncode=1,
        )
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.error == "real error"

    @pytest.mark.asyncio
    async def test_zero_exit_does_not_populate_error(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="warning on stderr but exit 0",
            returncode=0,
        )
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert result.status == "completed"
        assert result.error is None


# ---------------------------------------------------------------------------
# Stale-session detection
# ---------------------------------------------------------------------------


class TestPiBackendStaleSession:
    @pytest.mark.asyncio
    async def test_stale_session_in_stderr_raises(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="Error: session not found: abc123",
            returncode=1,
        )
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        with pytest.raises(StaleSessionError, match="stale"):
            await backend.execute_streaming(opts, {}, _make_collector()[1])

    @pytest.mark.asyncio
    async def test_no_such_session_pattern(self, tmp_path: Path) -> None:
        from modex_agent.agents.external_coding.agent import _is_stale_session

        assert _is_stale_session("ERROR: no such session: xyz")
        assert _is_stale_session("Session does not exist")
        assert _is_stale_session("session expired")

    @pytest.mark.asyncio
    async def test_non_stale_error_does_not_raise_stale(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="Error: out of memory",
            returncode=1,
        )
        backend = PiBackend()
        opts = ExecOptions(prompt="x", workdir=tmp_path)
        result = await backend.execute_streaming(opts, {}, _make_collector()[1])
        # Not a stale-session error — returns BackendResult with the error.
        assert result.status == "failed"
        assert "out of memory" in (result.error or "")

    @pytest.mark.asyncio
    async def test_stale_error_message_includes_session_path(
        self, tmp_path: Path, mock_os_layer: dict[str, object]
    ) -> None:
        session_path = "/my/resume/path.jsonl"
        mock_os_layer["process"] = FakeProcess(
            stdout_lines=[],
            stderr="session not found",
            returncode=1,
        )
        backend = PiBackend()
        opts = ExecOptions(
            prompt="x",
            workdir=tmp_path,
            resume_session_id=session_path,
        )
        with pytest.raises(StaleSessionError) as exc_info:
            await backend.execute_streaming(opts, {}, _make_collector()[1])
        assert session_path in str(exc_info.value)


# ---------------------------------------------------------------------------
# Manual smoke test (operator-run, not CI)
# ---------------------------------------------------------------------------


class TestPiBackendManualSmoke:
    """``@pytest.mark.manual`` — documents the real-CLI verification path.

    These tests are always skipped in CI. An operator runs them
    manually after installing ``pi`` on PATH to verify the backend
    against a real CLI.
    """

    @pytest.mark.manual
    def test_pi_real_cli_smoke_documentation(self, tmp_path: Path) -> None:
        """SMOKE TEST — real Pi CLI verification (operator-run).

        Prerequisites:
        1. ``pi`` installed and on ``PATH``.
        2. A writable workdir for the session file.

        Steps:
        1. Construct ``PiBackend()`` with default settings.
        2. Build ``ExecOptions(prompt="hello", workdir=<tmp>)``.
        3. Call ``await backend.execute_streaming(opts, env, on_emission)``
           with a collector callback.
        4. Verify the backend returns ``BackendResult(status="completed")``.
        5. Verify at least one ``TEXT_DELTA`` emission was forwarded.
        6. Verify the session file exists at the returned ``session_id`` path.

        Run with::

            pytest -m manual tests/unit/agents/external_coding/providers/test_pi_backend.py -s
        """
        pytest.skip(
            "Manual smoke test — requires real 'pi' CLI on PATH. Run with: pytest -m manual ..."
        )
