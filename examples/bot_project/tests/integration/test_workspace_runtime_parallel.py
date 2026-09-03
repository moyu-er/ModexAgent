"""Ticket 17 — multi-workspace parallel execution + WebUI runtime creation.

One real-service harness (``BotService`` boots from a workspace-layer scope
declaration; ONLY the LLM is scripted, patched onto
``BotModelProvider.chat_stream``) drives three scenarios:

1. **Runtime creation (the WebUI road)** — ``create_workspace`` writes the
   per-workspace declaration under ``config/scopes/workspaces/``, then
   materializes the workspace through the SAME lazy-materialization road a
   ``/cd``-switched workspace takes (no hot assembly — N2). The new
   workspace is immediately chat-capable: a turn driven through the real
   dispatcher reaches the output adapter, zero restart. Its backend
   selection differs from the service view, proving the materialization
   booted THE declaration (a bot.yml boot would have kept SQLITE).
2. **Restart persistence** — a full service stop + re-boot on the same
   project re-registers the created workspace from its declaration (the
   registry store cache is wiped, so the declaration is the only possible
   source) and reassembles it equivalently — same pools, chat works again.
3. **Parallel isolation (four-layer invariants)** — two workspaces run
   turns CONCURRENTLY: the scripted LLM blocks each turn's first round
   until BOTH turns are in flight (serialized execution would deadlock the
   barrier and time out), then each turn writes through its own
   workspace's REAL machinery — the ``write`` tool (per-workspace root
   provider), the memory system (per-workspace pool data), and the
   ctxvar-routed transcript/media stores (per-turn ``bind_workspace_root``).
   Every probe lands in its own workspace; zero cross-references. The
   eviction cap stays dormant (D1): ``max_materialized`` is unset and both
   workspaces remain materialized with both turns completed.

Session-level routing (AC f): two conversations in ONE channel (the same
input adapter), each carrying its own workspace, route to their own
workspaces' pools — replies land on the right session ids and each turn
ran under its own workspace root.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.core import BotService
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from bot.workspace.dynamic_workspaces import (
    WorkspaceCreationError,
    WorkspaceExistsError,
    create_workspace,
)

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.types import InputMessage, LLMResponse, OutputMessage, ToolCall
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.workspace.runtime import resolve_workspace_root

pytestmark = pytest.mark.integration

_MAIN_POOL = "main"


# ---------------------------------------------------------------------------
# Scripted LLM (the only fake) + recording output adapter
# ---------------------------------------------------------------------------


def _last_user_content(messages: list[Any]) -> str:
    """Best-effort extract the last real user message content from a chat history."""
    last = ""
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if role == "user" and content:
            text = str(content)
            if text.startswith("<system-reminder>"):
                continue
            last = text
    return last


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


@dataclass
class _ScriptedLLM:
    """Drives ``BotModelProvider.chat_stream`` for every pool/agent/turn.

    Echo mode (creation/routing tests): reply ``echo:<last user content>``.

    Probe mode (parallel test): each first-round call records the
    workspace root the TURN is bound to, writes through the real
    ctxvar-routed transcript/media singletons, then blocks on a barrier
    until BOTH conversations' turns are in flight — the concurrency proof.
    The first round returns a ``write`` tool call with a RELATIVE path (the
    workspace-scoped tool must resolve it against the owning workspace
    root); the second round returns the final reply.
    """

    probe_mode: bool = False
    calls: int = 0
    turn_roots: dict[str, Path] = field(default_factory=dict)
    media_paths: dict[str, Path] = field(default_factory=dict)
    _arrived: set[str] = field(default_factory=set)
    _both_in_flight: asyncio.Event = field(default_factory=asyncio.Event)
    _tool_round_done: set[str] = field(default_factory=set)
    _media: WorkspaceScopedMediaStore = field(
        default_factory=lambda: WorkspaceScopedMediaStore(".modex")
    )
    _transcripts: WorkspaceScopedTranscriptStore = field(
        default_factory=lambda: WorkspaceScopedTranscriptStore(".modex")
    )

    async def chat_stream(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del kwargs
        self.calls += 1
        marker = _last_user_content(messages) or "unknown"
        self.turn_roots[marker] = resolve_workspace_root()
        if not self.probe_mode:
            text = f"echo:{marker}"
            if on_content_delta is not None:
                await on_content_delta(text)
            return LLMResponse(content=text)

        await self._probe(marker)
        self._arrived.add(marker)
        if len(self._arrived) >= 2:
            self._both_in_flight.set()
        # Block until both conversations' turns are simultaneously in
        # flight. Serialized turns deadlock here and time out — that is
        # the non-blocking-parallelism assertion.
        await asyncio.wait_for(self._both_in_flight.wait(), timeout=20.0)
        if "write" in _tool_names(tools) and marker not in self._tool_round_done:
            self._tool_round_done.add(marker)
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="write",
                        arguments={
                            "path": f"tool-probe-{marker}.txt",
                            "content": f"written-by-{marker}",
                        },
                        call_id=f"write-{marker}",
                    )
                ],
            )
        text = f"done-{marker}"
        if on_content_delta is not None:
            await on_content_delta(text)
        return LLMResponse(content=text)

    async def _probe(self, marker: str) -> None:
        """In-turn probes through the REAL ctxvar-routed business writers.

        The scripted LLM runs inside the turn, so the workspace root is
        bound exactly as it is for the emitter's transcript appends and the
        ingest stage's media saves in production.
        """
        session_id = f"probe-{marker}.{_MAIN_POOL}"
        await self._transcripts.append(
            session_id,
            UserMessageEvent(
                session_id=session_id,
                agent_name=_MAIN_POOL,
                content=f"transcript-probe-{marker}",
            ),
            pool=_MAIN_POOL,
        )
        self.media_paths[marker] = self._media.store_for(_MAIN_POOL).save(
            f"probe-{marker}", "probe", f"media-probe-{marker}".encode()
        )


class _EchoDefaultProvider(CallbackStreamProvider):
    """Service-default provider (memory summarizer / background): echoes."""

    def get_default_model(self) -> str:
        return "dummy-mini"

    async def chat_stream(
        self,
        messages: list[Any],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: object,
    ) -> LLMResponse:
        content = _last_user_content(messages)
        return LLMResponse(content=f"echo:{content}" if content else "echo:ok")


class _RecordingOutputAdapter(OutputAdapter):
    """Non-streaming adapter that records every ``(session_id, content)`` send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.PSEUDO

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sent.append((session_id, message.content or ""))

    async def send_delta(
        self,
        delta: str,
        session_id: str,
        metadata: dict[str, object] | None = None,
    ) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Harness: real BotService on a tmp project (only the LLM is scripted)
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    service: BotService
    input_adapter: WebSocketInputAdapter
    output_adapter: _RecordingOutputAdapter
    script: _ScriptedLLM
    router_task: asyncio.Task[None]
    project_dir: Path

    async def send(self, *, content: str, external_id: str, workspace: Path) -> str:
        session = SessionIdFactory().create(agent_name=_MAIN_POOL, external_id=external_id)
        self.input_adapter.put_input_message(
            InputMessage(content=content, session=session, workspace=workspace, channel="test")
        )
        return session.session_id

    def replies(self) -> list[str]:
        return [content for _, content in self.output_adapter.sent]

    async def wait_for_reply(self, needle: str, timeout: float = 30.0) -> None:
        await _wait_for(
            lambda: any(needle in content for content in self.replies()),
            f"reply containing {needle!r}",
            timeout=timeout,
        )

    async def stop(self) -> None:
        self.router_task.cancel()
        with contextlib.suppress(BaseException):
            await self.router_task
        await self.service.stop()


async def _wait_for(predicate: Any, what: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not reached within {timeout}s: {what}")


def _write_minimal_config(project_dir: Path) -> None:
    """A bootable one-pool project: workspace-layer scope declaration + model."""
    config_dir = project_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)

    # BIZ components (execution strategies) are directory-discovered from
    # <project>/plugins by BotService — a bootable project carries the real
    # plugin set, so the synthetic one must too.
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "plugins",
        project_dir / "plugins",
        dirs_exist_ok=True,
    )

    (project_dir / "agents" / "main.md").write_text(
        "You are a helpful assistant. Reply briefly.\n", encoding="utf-8"
    )

    (config_dir / "bot_config.yml").write_text(
        """
safety:
  llm: {request_timeout: 45.0, stream_idle_timeout: 90.0, max_retries: 1, retry_backoff: [2.0, 8.0]}
  turn: {hook_timeout: 10.0, tool_timeout: 30.0}
paths:
  data_dir_name: ".modex"
""",
        encoding="utf-8",
    )

    # The workspace layer selects the multi-live stack (ticket 14); the
    # single ``main`` pool boots the declaration road (ticket 11). The
    # root agent's toolset defaults to the root position (full) — the
    # ``write`` tool is what the isolation probe drives.
    (config_dir / "scopes").mkdir(parents=True, exist_ok=True)
    (config_dir / "scopes" / "bot.yml").write_text(
        """
workspace:
  name: parallel-test
  pools:
    main:
      agents:
        main:
          description: test main agent
""",
        encoding="utf-8",
    )

    (config_dir / "model.yml").write_text(
        """
default_provider: dummy
default_model: dummy-mini
max_context_tokens: 32000
providers:
  - key: dummy
    name: dummy
    url: http://localhost
    api_key: dummy
    models:
      - name: dummy-mini
        model: openai/dummy-mini
        capabilities: [text]
        temperature: 0.7
        max_output_tokens: 1000
""",
        encoding="utf-8",
    )

    # No shared MCP registry and no servers: zero subprocesses.
    mcp_dir = config_dir / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "registry.json").write_text(
        '{"sharedRegistry": false}', encoding="utf-8"
    )


async def _boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, probe_mode: bool = False
) -> _Harness:
    """Boot a real BotService on the tmp project with a scripted LLM."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modexctl.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))

    _write_minimal_config(tmp_path)
    app_config = AppConfig.from_yaml(tmp_path / "config" / "bot_config.yml")

    input_adapter = WebSocketInputAdapter()
    output_adapter = _RecordingOutputAdapter()

    def emitter_factory(session_id: str, pool: str) -> StreamingAwareEmitter:
        assert pool == _MAIN_POOL
        return StreamingAwareEmitter(output_adapter, session_id)

    service = BotService(
        config_dir=tmp_path / "config",
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        emitter_factory=emitter_factory,
        app_config=app_config,
    )
    # Base BotService does not set _transcript_store (WebUIService-only),
    # but _build_resources reads it — None keeps materialize working.
    service._transcript_store = None

    script = _ScriptedLLM(probe_mode=probe_mode)

    import bot.service.core as core_mod
    from bot.service.model_provider import BotModelProvider

    monkeypatch.setattr(
        core_mod.BotService, "_build_default_provider", lambda self: _EchoDefaultProvider()
    )
    monkeypatch.setattr(
        core_mod.BotService, "_project_dir", property(lambda self: tmp_path)
    )

    async def _scripted_chat_stream(
        self: BotModelProvider,
        messages: list[Any],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        del self, model, temperature, max_output_tokens, on_reasoning_delta
        return await script.chat_stream(
            messages, tools=tools, on_content_delta=on_content_delta, **kwargs
        )

    monkeypatch.setattr(BotModelProvider, "chat_stream", _scripted_chat_stream)

    await service.initialize()
    await input_adapter.start()
    router_task = asyncio.create_task(service.workspace_stack.dispatcher.run())
    return _Harness(
        service=service,
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        script=script,
        router_task=router_task,
        project_dir=tmp_path,
    )


def _files_containing(root: Path, needle: str) -> list[Path]:
    """Every file under ``root`` whose bytes contain ``needle``."""
    if not root.exists():
        return []
    hits: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                if needle in path.read_text(errors="replace"):
                    hits.append(path)
            except OSError:
                continue
    return hits


# ---------------------------------------------------------------------------
# 1. Runtime creation: write declaration → full boot path → chat, no restart
# ---------------------------------------------------------------------------


async def test_create_workspace_full_chain_no_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await _boot(tmp_path, monkeypatch)
    try:
        service = harness.service
        registry = service.workspace_stack.registry
        assert registry.materialized_count() == 1  # home only

        # backend="file" differs from the service view (SQLITE default) —
        # the observable proof that alpha's materialization booted ITS
        # declaration rather than the primary.
        result = await create_workspace(service, name="alpha", backend="file")

        # The declaration was written back and is directly loadable.
        declaration_path = tmp_path / "config" / "scopes" / "workspaces" / "alpha.yml"
        assert result.declaration_path == declaration_path
        assert declaration_path.is_file()
        spec = load_scope_declaration(declaration_path)
        assert spec.workspace is not None
        assert spec.workspace.name == "alpha"
        assert spec.workspace.persistence is not None
        assert spec.workspace.persistence.backend.value == "file"
        # The hosted pools are the primary declaration's, copied verbatim
        # (model-level equality — no field lost in the YAML round trip).
        primary = load_scope_declaration(tmp_path / "config" / "scopes" / "bot.yml")
        assert primary.workspace is not None
        assert [p.model_dump() for p in spec.workspace.pools] == [
            p.model_dump() for p in primary.workspace.pools
        ]

        # The workspace materialized through the registry (the full boot
        # path — same road a /cd target takes), with its pools booted and
        # their output bridges running (a silent workspace would mean
        # turns that never deliver).
        assert registry.materialized_count() == 2
        root = result.root
        assert root == tmp_path / "subworkspace" / "alpha"
        assert root.is_dir()
        resources = await registry.materialize(
            await registry.get_or_open(root)
        )
        assert set(resources.pools) == {_MAIN_POOL}
        for pool_instance in resources.pools.values():
            assert pool_instance.broker_bridge._tasks, "new workspace pool bridge not running"
        # The backend selection from the declaration took effect (the
        # service view is SQLITE; alpha declared FILE).
        assert resources.persistence is None
        assert service._home_resources.persistence is not None

        # Chat works without restart: a turn through the real dispatcher
        # into the new workspace reaches the output adapter.
        await harness.send(content="hello-alpha", external_id="conv-alpha", workspace=root)
        await harness.wait_for_reply("echo:hello-alpha")
    finally:
        await harness.stop()


async def test_create_workspace_validation_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await _boot(tmp_path, monkeypatch)
    try:
        service = harness.service

        for bad_name in ("", "a/b", "../escape", ".hidden", "x" * 65, "sp ace"):
            with pytest.raises(WorkspaceCreationError, match="invalid workspace name"):
                await create_workspace(service, name=bad_name)

        with pytest.raises(WorkspaceCreationError, match="unknown persistence backend"):
            await create_workspace(service, name="alpha", backend="postgres")

        # A registered workspace already sitting at the dynamic root is a
        # collision, even without a declaration file (e.g. someone /cd'd
        # into subworkspace/beta earlier).
        beta_root = tmp_path / "subworkspace" / "beta"
        beta_root.mkdir(parents=True)
        await service.workspace_stack.registry.get_or_open(beta_root)
        with pytest.raises(WorkspaceExistsError):
            await create_workspace(service, name="beta")

        # The same name twice is a collision.
        await create_workspace(service, name="alpha")
        with pytest.raises(WorkspaceExistsError):
            await create_workspace(service, name="alpha")

        # A failed materialization rolls the declaration back — a broken
        # half-created workspace must not poison the next restart's boot
        # read of workspaces/*.yml.
        from modex_agent.workspace.registry import ScopeRegistry

        async def _boom(self: Any, ctx: Any) -> Any:
            del self, ctx
            raise RuntimeError("simulated materialization failure")

        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(ScopeRegistry, "materialize", _boom)
            with pytest.raises(RuntimeError, match="simulated materialization failure"):
                await create_workspace(service, name="gamma")
        assert not (tmp_path / "config" / "scopes" / "workspaces" / "gamma.yml").exists()
        assert not (tmp_path / "subworkspace" / "gamma").exists()
    finally:
        await harness.stop()


# ---------------------------------------------------------------------------
# 2. Restart persistence: the declaration is the durable record
# ---------------------------------------------------------------------------


async def test_created_workspace_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await _boot(tmp_path, monkeypatch)
    result = await create_workspace(harness.service, name="alpha")
    pre_pools = set(result.pools)
    root = result.root.resolve()
    await harness.stop()

    # Wipe the registry store cache — the declaration under
    # config/scopes/workspaces/ is now the ONLY record of the workspace.
    with contextlib.suppress(FileNotFoundError):
        (tmp_path / ".modex" / "_registry" / "state.db").unlink()

    harness2 = await _boot(tmp_path, monkeypatch)
    try:
        registry = harness2.service.workspace_stack.registry
        # Boot re-registered the workspace from its declaration (the
        # registry store was wiped, so this cannot be a cached record).
        assert root in {Path(t).resolve() for t in registry.known_targets()}

        # Same reassembly: lazily materialize on demand — same pools, the
        # declaration's persistence selection re-applied.
        resources = await registry.materialize(await registry.get_or_open(root))
        assert set(resources.pools) == pre_pools
        assert resources.persistence is not None  # backend inherited (SQLITE)

        # And it chats again.
        await harness2.send(content="hello-again", external_id="conv-alpha-2", workspace=root)
        await harness2.wait_for_reply("echo:hello-again")
    finally:
        await harness2.stop()


# ---------------------------------------------------------------------------
# 3. Parallel isolation: concurrent turns, four probes, capacity dormant
# ---------------------------------------------------------------------------


async def test_parallel_turns_isolated_concurrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await _boot(tmp_path, monkeypatch, probe_mode=True)
    try:
        registry = harness.service.workspace_stack.registry
        ws_a = tmp_path / "ws_a"
        ws_b = tmp_path / "ws_b"
        ws_a.mkdir()
        ws_b.mkdir()
        # Materialize both up front so turn timing measures execution, not
        # assembly (creation-time materialization is covered above).
        await registry.materialize(await registry.get_or_open(ws_a))
        await registry.materialize(await registry.get_or_open(ws_b))

        # Layer ①: per-workspace resource bundles with distinct brokers and
        # distinct pool objects — structurally separate execution stacks.
        resources_a = next(
            r for r in registry.iter_materialized_resources() if r.target == ws_a.resolve()
        )
        resources_b = next(
            r for r in registry.iter_materialized_resources() if r.target == ws_b.resolve()
        )
        assert resources_a.broker is not resources_b.broker
        assert resources_a.pools[_MAIN_POOL] is not resources_b.pools[_MAIN_POOL]

        # Capacity is dormant (D1): no cap configured, nothing evicted.
        assert registry._max_materialized is None

        marker_a, marker_b = "ws-a-hi", "ws-b-hi"
        sid_a = await harness.send(content=marker_a, external_id="conv-a", workspace=ws_a)
        sid_b = await harness.send(content=marker_b, external_id="conv-b", workspace=ws_b)

        # Both turns complete (the barrier inside the scripted LLM would
        # deadlock serialized execution — reaching the replies at all
        # proves the two turns ran concurrently without blocking).
        await harness.wait_for_reply(f"done-{marker_a}")
        await harness.wait_for_reply(f"done-{marker_b}")

        # Layer ③: each turn was bound to ITS workspace root (the scripted
        # LLM records resolve_workspace_root() inside the turn).
        assert harness.script.turn_roots[marker_a] == ws_a.resolve()
        assert harness.script.turn_roots[marker_b] == ws_b.resolve()

        # Layer ②: the write tool's relative path resolved against the
        # OWNING workspace root — no cross-landing.
        assert (ws_a / f"tool-probe-{marker_a}.txt").read_text() == f"written-by-{marker_a}"
        assert (ws_b / f"tool-probe-{marker_b}.txt").read_text() == f"written-by-{marker_b}"
        assert not (ws_a / f"tool-probe-{marker_b}.txt").exists()
        assert not (ws_b / f"tool-probe-{marker_a}.txt").exists()

        # Memory writes: read each workspace's REAL session memory back
        # through its own memory system (the same read path the next
        # turn's context assembly takes) — each conversation's history is
        # there, and the other workspace's memory never saw it.
        from modex_agent.core.scope import MemoryContext

        async def _history_text(resources: Any, session_id: str) -> str:
            memory_system = resources.pool_data[_MAIN_POOL].context_manager.memory_system
            history = await memory_system.get_full_history(
                MemoryContext(session_id=session_id), limit=100
            )
            return " ".join(str(getattr(m, "content", "") or "") for m in history)

        async def _wait_for_history(
            resources: Any, session_id: str, needle: str
        ) -> str:
            deadline = asyncio.get_running_loop().time() + 30.0
            while asyncio.get_running_loop().time() < deadline:
                text = await _history_text(resources, session_id)
                if needle in text:
                    return text
                await asyncio.sleep(0.1)
            raise AssertionError(
                f"session memory never persisted {needle!r} within 30s"
            )

        text_a = await _wait_for_history(resources_a, sid_a, marker_a)
        text_b = await _wait_for_history(resources_b, sid_b, marker_b)
        assert marker_b not in text_a
        assert marker_a not in text_b

        # Transcript + media: the ctxvar-routed business writers landed
        # under each workspace's own sessions/media trees — zero cross.
        transcript_a = ws_a / ".modex" / "sessions" / _MAIN_POOL / f"probe-{marker_a}.{_MAIN_POOL}.jsonl"
        transcript_b = ws_b / ".modex" / "sessions" / _MAIN_POOL / f"probe-{marker_b}.{_MAIN_POOL}.jsonl"
        assert transcript_a.is_file(), f"transcript probe missing: {transcript_a}"
        assert transcript_b.is_file(), f"transcript probe missing: {transcript_b}"
        assert _files_containing(ws_a / ".modex" / "sessions", marker_b) == []
        assert _files_containing(ws_b / ".modex" / "sessions", marker_a) == []
        assert harness.script.media_paths[marker_a].is_relative_to(
            ws_a / ".modex" / "media" / _MAIN_POOL
        )
        assert harness.script.media_paths[marker_b].is_relative_to(
            ws_b / ".modex" / "media" / _MAIN_POOL
        )
        assert harness.script.media_paths[marker_a].read_bytes() == f"media-probe-{marker_a}".encode()
        assert harness.script.media_paths[marker_b].read_bytes() == f"media-probe-{marker_b}".encode()

        # AC (c): both turns completed with capacity dormant — neither
        # workspace was evicted while the other ran (home is the third
        # materialized bundle).
        assert registry.materialized_count() == 3
        materialized = {
            Path(t).resolve() for t in registry._resources  # type: ignore[attr-defined]
        }
        assert materialized >= {ws_a.resolve(), ws_b.resolve()}
    finally:
        await harness.stop()


# ---------------------------------------------------------------------------
# 4. Session-level routing: two conversations, one channel, two workspaces
# ---------------------------------------------------------------------------


async def test_two_sessions_one_channel_route_to_their_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await _boot(tmp_path, monkeypatch)
    try:
        registry = harness.service.workspace_stack.registry
        ws_a = tmp_path / "ws_a"
        ws_b = tmp_path / "ws_b"
        ws_a.mkdir()
        ws_b.mkdir()
        await registry.materialize(await registry.get_or_open(ws_a))
        await registry.materialize(await registry.get_or_open(ws_b))

        # Both conversations arrive through the SAME channel (the same
        # input adapter), each message carrying its conversation's
        # workspace — the per-conversation pointer.
        session_a = SessionIdFactory().create(agent_name=_MAIN_POOL, external_id="conv-a")
        session_b = SessionIdFactory().create(agent_name=_MAIN_POOL, external_id="conv-b")
        harness.input_adapter.put_input_message(
            InputMessage(content="route-a", session=session_a, workspace=ws_a, channel="test")
        )
        harness.input_adapter.put_input_message(
            InputMessage(content="route-b", session=session_b, workspace=ws_b, channel="test")
        )

        await harness.wait_for_reply("echo:route-a")
        await harness.wait_for_reply("echo:route-b")

        # Each reply was delivered on its own conversation's session id —
        # routing never crossed the two conversations.
        session_replies = dict(harness.output_adapter.sent)
        assert session_replies[session_a.session_id] == "echo:route-a"
        assert session_replies[session_b.session_id] == "echo:route-b"

        # Each conversation's turn ran under its own workspace root.
        assert harness.script.turn_roots["route-a"] == ws_a.resolve()
        assert harness.script.turn_roots["route-b"] == ws_b.resolve()
    finally:
        await harness.stop()
