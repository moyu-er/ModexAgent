"""Unit tests for child session event routing in the external plane.

The routing logic lives in :class:`ExternalEventNormalizer`; these tests
drive it end-to-end through :class:`ExternalAgent` with a
:class:`ScriptedTransport` whose steps carry ``source_session_id``.

Coverage (Todo 5 acceptance criteria):

1. **Happy path** — main text → main sink; child text → child sink;
   child tool → child sink; main text → main sink. Discovery sink
   called, mapping committed.
2. **Race condition** — first child event triggers discovery
   synchronously inside the normalizer; the same delivery routes to the
   newly created child sink. No drop.
3. **Backward compat** — agent constructed without discovery
   collaborators → child events dropped with warning (no crash).
4. **Orphan isolation** — child tool result resolves from the child's
   own open-call set; the main session's call ids are untouched.
5. **Resume** — two turns with the same ``provider_child_sid`` →
   deterministic modex session id; ``on_child_discovered`` fires each
   turn (per-turn routes are reinitialized).
6. **Lifecycle** — after the turn, the normalizer's child routes are
   cleared and pending tasks gathered.
7. **Child env snapshot** — on child discovery, a per-provider-session
   env snapshot file is written so modexctl (called by the child) can
   read the child's MODEX_* vars.
8. **Concurrent turn isolation** — two sessions on two agents cannot
   cross over.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from modex_agent.agents.external.agent import ExternalAgent
from modex_agent.agents.external.backend_provider import (
    PoolScopedBackendProvider,
)
from modex_agent.agents.external.child_discovery import (
    ChildSessionDiscoverySink,
)
from modex_agent.agents.external.paths import ExternalPaths
from modex_agent.agents.external.session_store import (
    LocalFileExternalSessionMapStore,
)
from modex_agent.agents.external.transports import (
    ScriptedProgramme,
    ScriptedStep,
    ScriptedTransport,
)
from modex_agent.agents.external.types import (
    BackendStatus,
    ExternalEnvSpec,
)
from modex_agent.core.agent import AgentCommKind, AgentContext, ProviderKind
from modex_agent.core.emitter import (
    AgentResult,
    TurnBinding,
    TurnEventSink,
    TurnEventSinkFactory,
)
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.turn_events import (
    StopReason,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.persistence.session_registry import SessionRegistry
from modex_agent.tools.manager import InMemoryToolManager

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingEmitter(TurnEventSink):
    """Main-session sink capturing turn events, terminal, errors."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas: list[str] = []
        self.turn_events: list[TurnEvent] = []
        self.completed: AgentResult | None = None
        self.errors: list[str] = []

    def wants_streaming(self) -> bool:
        return False

    async def _dispatch(self, event: TurnEvent) -> None:
        self.turn_events.append(event)
        match event:
            case TurnTextEvent(text=text):
                self.deltas.append(text)
            case TurnErroredEvent(message=message):
                self.errors.append(message)
            case TurnFinishedEvent(stop_reason=stop_reason, error=error):
                self.completed = AgentResult(error=error, stop_reason=stop_reason)


class _RecordingChildEmitter(TurnEventSink):
    """Child-session sink — records the modex_sid it was created for."""

    def __init__(self, modex_sid: str) -> None:
        super().__init__()
        self.modex_sid = modex_sid
        self.turn_events: list[TurnEvent] = []

    async def _dispatch(self, event: TurnEvent) -> None:
        self.turn_events.append(event)


class _MockSink(ChildSessionDiscoverySink):
    """Deterministic discovery sink — maps provider_child_sid to mock_modex_<id>."""

    def __init__(self) -> None:
        self.discovered: list[tuple[str, str]] = []
        self.resolve_calls: list[str] = []

    async def on_child_discovered(
        self,
        provider_child_session_id: str,
        parent_modex_session_id: str,
        provider_agent_type: str | None = None,
    ) -> str:
        self.discovered.append((provider_child_session_id, parent_modex_session_id))
        return self.resolve_child_modex_session_id(provider_child_session_id)

    def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
        self.resolve_calls.append(provider_child_session_id)
        return f"mock_modex_{provider_child_session_id}"


class _MockRegistry(SessionRegistry):
    """In-memory SessionRegistry tracking register calls for resume tests."""

    def __init__(self) -> None:
        self.register_calls: list[SessionInfo] = []

    async def register(self, session: SessionInfo) -> None:
        self.register_calls.append(session)

    async def get(self, session_id: str) -> SessionInfo | None:
        return None

    async def touch(self, session_id: str) -> None:
        pass

    async def load_all(self) -> None:
        pass

    async def cleanup(self, session_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(workdir: Path, session_id: str = "pool1.agent1") -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=workdir / "inbox",
        workdir=workdir,
        session_id=session_id,
        agent_name="agent1",
        provider_session_id="prov-initial",
        agent_pool_map={"agent1": "pool1", "helper": "pool1"},
        targets=[("helper", "a helper agent")],
        modexctl_bin_dir=workdir / "bin",
    )


def _make_ctx(session_id: str = "pool1.agent1") -> AgentContext:
    history = ListMessageHistory([ChatMessage(role=MessageRole.USER, content="run")])
    return AgentContext(
        system_prompt="",
        history=history,
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        current_input="run",
    )


def _make_child_emitter_factory(
    emitters: dict[str, _RecordingChildEmitter],
) -> TurnEventSinkFactory:
    def factory(binding: TurnBinding) -> TurnEventSink:
        emitter = _RecordingChildEmitter(binding.session_id)
        emitters[binding.session_id] = emitter
        return emitter

    return factory


def _text(text: str, source: str | None = None) -> ScriptedStep:
    return ScriptedStep(event=TurnTextEvent(text=text), source_session_id=source)


def _tool_use(call_id: str, tool_name: str = "bash", source: str | None = None) -> ScriptedStep:
    return ScriptedStep(
        event=TurnToolCallEvent(tool_name=tool_name, call_id=call_id, arguments={"cmd": "ls"}),
        source_session_id=source,
    )


def _tool_result(call_id: str, output: str = "ok", source: str | None = None) -> ScriptedStep:
    return ScriptedStep(
        event=TurnToolResultEvent(tool_name="bash", call_id=call_id, output=output),
        source_session_id=source,
    )


def _build_transport(steps: list[ScriptedStep]) -> ScriptedTransport:
    return ScriptedTransport(
        ScriptedProgramme(
            steps=tuple(steps),
            status=BackendStatus.COMPLETED,
            session_id="prov-sess-1",
        )
    )


def _build_agent(
    tmp_path: Path,
    transport: ScriptedTransport,
    *,
    sink: ChildSessionDiscoverySink | None = None,
    registry: SessionRegistry | None = None,
    emitter_factory: TurnEventSinkFactory | None = None,
) -> ExternalAgent:
    return ExternalAgent(
        backend_provider=PoolScopedBackendProvider(transport),
        session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
        provider_kind=ProviderKind.OPENCODE,
        spec=_make_spec(tmp_path),
        base_env={"PATH": "/usr/bin"},
        child_discovery_sink=sink,
        session_registry=registry,
        child_emitter_factory=emitter_factory,
    )


# ---------------------------------------------------------------------------
# 1. Happy path — main + child interleaved events routed correctly
# ---------------------------------------------------------------------------


class TestHappyPathRouting:
    async def test_main_and_child_events_routed_to_correct_emitters(
        self, tmp_path: Path
    ) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        transport = _build_transport(
            [
                _text("main text 1"),
                _text("child text", source="child_1"),
                _tool_use("c-child", source="child_1"),
                _text("main text 2"),
            ]
        )
        agent = _build_agent(
            tmp_path,
            transport,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        result = await agent.run(_make_ctx(), main_emitter)

        assert result.stop_reason == StopReason.COMPLETED

        # Main emitter received only main-session text events.
        main_texts = [e for e in main_emitter.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in main_texts] == ["main text 1", "main text 2"]

        # Child emitter received child text + tool call.
        assert "mock_modex_child_1" in child_emitters
        child = child_emitters["mock_modex_child_1"]
        assert child.turn_events[0] == TurnTextEvent(text="child text")
        assert isinstance(child.turn_events[1], TurnToolCallEvent)
        assert child.turn_events[1].tool_name == "bash"

        # Discovery sink was called once for child_1 with the parent sid.
        assert sink.discovered == [("child_1", "pool1.agent1")]
        # resolve called once by the normalizer (sync step) + once by mock
        # sink's on_child_discovered (async background step).
        assert sink.resolve_calls == ["child_1", "child_1"]


# ---------------------------------------------------------------------------
# 2. Race condition — first child event routed in the same call
# ---------------------------------------------------------------------------


class TestRaceConditionNoDrop:
    async def test_first_child_event_not_dropped(self, tmp_path: Path) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        # Only one event — a child text delta. If discovery were async,
        # this event would be dropped before the child sink exists.
        transport = _build_transport([_text("only child", source="child_x")])
        agent = _build_agent(
            tmp_path,
            transport,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        await agent.run(_make_ctx(), main_emitter)

        # The child sink was created and received the text.
        assert "mock_modex_child_x" in child_emitters
        child = child_emitters["mock_modex_child_x"]
        assert child.turn_events == [TurnTextEvent(text="only child")]

        # Main sink received no child CONTENT (terminal bookkeeping is fine).
        assert [e for e in main_emitter.turn_events if isinstance(e, TurnTextEvent)] == []

        # Discovery ran.
        assert sink.discovered == [("child_x", "pool1.agent1")]


# ---------------------------------------------------------------------------
# 3. Backward compat — no discovery collaborators → child dropped, no crash
# ---------------------------------------------------------------------------


class TestBackwardCompatNoCollaborators:
    async def test_child_events_dropped_gracefully(self, tmp_path: Path) -> None:
        transport = _build_transport(
            [
                _text("main text"),
                _text("child text", source="child_1"),
                _text("more main"),
            ]
        )
        # No child_discovery_sink, no child_emitter_factory.
        agent = _build_agent(tmp_path, transport)
        main_emitter = _RecordingEmitter()

        result = await agent.run(_make_ctx(), main_emitter)

        assert result.stop_reason == StopReason.COMPLETED

        # Main emitter received only the two main text deltas.
        main_texts = [e for e in main_emitter.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in main_texts] == ["main text", "more main"]


# ---------------------------------------------------------------------------
# 4. Call-id isolation — child tool result does not resolve from main
# ---------------------------------------------------------------------------


class TestCallIdIsolation:
    async def test_child_tool_result_resolves_from_child_route(self, tmp_path: Path) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        transport = _build_transport(
            [
                _tool_use("c-child", tool_name="grep", source="child_1"),
                _tool_use("c-main"),
                # Tool result for the child call_id → resolves from the child route.
                _tool_result("c-child", output="child output", source="child_1"),
                # Tool result for the main call_id → resolves from the main route.
                _tool_result("c-main", output="main output"),
            ]
        )
        agent = _build_agent(
            tmp_path,
            transport,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        await agent.run(_make_ctx(), main_emitter)

        # Main emitter received the main TOOL_CALL + TOOL_RESULT.
        main_tool_calls = [
            e for e in main_emitter.turn_events if isinstance(e, TurnToolCallEvent)
        ]
        main_tool_results = [
            e for e in main_emitter.turn_events if isinstance(e, TurnToolResultEvent)
        ]
        assert len(main_tool_calls) == 1
        assert main_tool_calls[0].call_id == "c-main"
        assert len(main_tool_results) == 1
        assert main_tool_results[0].call_id == "c-main"
        assert main_tool_results[0].output == "main output"

        # Child emitter received the child TOOL_CALL + TOOL_RESULT.
        child = child_emitters["mock_modex_child_1"]
        child_tool_calls = [
            e for e in child.turn_events if isinstance(e, TurnToolCallEvent)
        ]
        child_tool_results = [
            e for e in child.turn_events if isinstance(e, TurnToolResultEvent)
        ]
        assert len(child_tool_calls) == 1
        assert child_tool_calls[0].call_id == "c-child"
        assert child_tool_calls[0].tool_name == "grep"
        assert len(child_tool_results) == 1
        assert child_tool_results[0].call_id == "c-child"
        assert child_tool_results[0].output == "child output"


# ---------------------------------------------------------------------------
# 5. Resume — two turns, same provider_child_sid → deterministic modex_sid
# ---------------------------------------------------------------------------


class TestResumeDeterministicModexSid:
    async def test_two_turns_same_child_deterministic_sid(self, tmp_path: Path) -> None:
        sink = _MockSink()
        registry = _MockRegistry()
        child_emitters: dict[str, _RecordingChildEmitter] = {}

        # Turn 1
        agent = _build_agent(
            tmp_path,
            _build_transport([_text("turn1 child", source="child_1")]),
            sink=sink,
            registry=registry,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        assert sink.discovered == [("child_1", "pool1.agent1")]
        assert sink.resolve_calls == ["child_1", "child_1"]
        turn_1_child = child_emitters["mock_modex_child_1"]
        assert len(turn_1_child.turn_events) == 1

        # Turn 2 — same agent, same provider_child_sid.
        # Per-turn routes are reinitialized, so discovery runs again.
        agent._backend_provider = PoolScopedBackendProvider(
            _build_transport([_text("turn2 child", source="child_1")])
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        # on_child_discovered fired again (per-turn routes reinitialized).
        assert sink.discovered == [
            ("child_1", "pool1.agent1"),
            ("child_1", "pool1.agent1"),
        ]
        assert sink.resolve_calls == ["child_1", "child_1", "child_1", "child_1"]

        # Deterministic modex_sid — same key both turns.
        turn_2_child = child_emitters["mock_modex_child_1"]
        assert turn_2_child is not turn_1_child
        assert len(turn_2_child.turn_events) == 1
        assert turn_2_child.turn_events[0] == TurnTextEvent(text="turn2 child")


# ---------------------------------------------------------------------------
# 6. Lifecycle — pending discovery tasks gathered before turn end
# ---------------------------------------------------------------------------


class TestEmitterLifecycle:
    async def test_pending_child_tasks_gathered_before_release(self, tmp_path: Path) -> None:
        # A sink whose on_child_discovered blocks until released — proves
        # the turn's finally block awaits it before returning.
        release_event = asyncio.Event()

        class _BlockingSink(ChildSessionDiscoverySink):
            def __init__(self) -> None:
                self.discovered: list[tuple[str, str]] = []

            async def on_child_discovered(
                self,
                provider_child_session_id: str,
                parent_modex_session_id: str,
                provider_agent_type: str | None = None,
            ) -> str:
                await release_event.wait()
                self.discovered.append((provider_child_session_id, parent_modex_session_id))
                return self.resolve_child_modex_session_id(provider_child_session_id)

            def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
                return f"mock_modex_{provider_child_session_id}"

        sink = _BlockingSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        transport = _build_transport([_text("child text", source="child_1")])
        agent = _build_agent(
            tmp_path,
            transport,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )

        # Run the turn in a task so we can control the blocking sink.
        run_task = asyncio.create_task(agent.run(_make_ctx(), _RecordingEmitter()))

        # Let the transport replay events and create the discovery task.
        # The finally block will block on gather(pending_tasks).
        await asyncio.sleep(0.1)

        # The turn hasn't completed because the discovery task is blocked.
        assert not run_task.done()

        # Release the sink — the gather completes, the turn finishes.
        release_event.set()
        result = await asyncio.wait_for(run_task, timeout=2.0)

        assert result.stop_reason == StopReason.COMPLETED
        assert sink.discovered == [("child_1", "pool1.agent1")]


# ---------------------------------------------------------------------------
# 7. Child env snapshot — per-provider-session file written on discovery
# ---------------------------------------------------------------------------


class TestChildEnvSnapshotOnDiscovery:
    """On first discovery of a child session, the normalizer writes a
    per-provider-session env snapshot file so modexctl (when invoked by the
    child) can read the child's MODEX_* vars."""

    async def _drive(self, tmp_path: Path, steps: list[ScriptedStep]) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        agent = _build_agent(
            tmp_path,
            _build_transport(steps),
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

    async def test_child_snapshot_file_written_on_discovery(self, tmp_path: Path) -> None:
        await self._drive(tmp_path, [_text("child text", source="child_1")])

        paths = ExternalPaths(tmp_path)
        snapshot_path = paths.env_snapshot_for_session("child_1")
        assert snapshot_path.exists(), "child env snapshot file not written"

    async def test_child_snapshot_contains_child_modex_session_id(self, tmp_path: Path) -> None:
        await self._drive(tmp_path, [_text("child text", source="child_1")])

        paths = ExternalPaths(tmp_path)
        snapshot = json.loads(paths.env_snapshot_for_session("child_1").read_text(encoding="utf-8"))
        # _MockSink.resolve_child_modex_session_id returns
        # "mock_modex_<provider_child_sid>".
        assert snapshot["MODEX_SESSION_ID"] == "mock_modex_child_1"

    async def test_child_snapshot_contains_parent_session_id(self, tmp_path: Path) -> None:
        await self._drive(tmp_path, [_text("child text", source="child_1")])

        paths = ExternalPaths(tmp_path)
        snapshot = json.loads(paths.env_snapshot_for_session("child_1").read_text(encoding="utf-8"))
        assert snapshot["MODEX_PARENT_SESSION_ID"] == "pool1.agent1"

    async def test_child_snapshot_comm_kind_is_subagent(self, tmp_path: Path) -> None:
        await self._drive(tmp_path, [_text("child text", source="child_1")])

        paths = ExternalPaths(tmp_path)
        snapshot = json.loads(paths.env_snapshot_for_session("child_1").read_text(encoding="utf-8"))
        assert snapshot["MODEX_COMM_KIND"] == AgentCommKind.SUBAGENT.value

    async def test_child_snapshot_contains_provider_child_sid(self, tmp_path: Path) -> None:
        await self._drive(tmp_path, [_text("child text", source="child_1")])

        paths = ExternalPaths(tmp_path)
        snapshot = json.loads(paths.env_snapshot_for_session("child_1").read_text(encoding="utf-8"))
        assert snapshot["MODEX_PROVIDER_SESSION_ID"] == "child_1"

    async def test_child_snapshot_written_once_per_child(self, tmp_path: Path) -> None:
        """Multiple events from the same child session must not rewrite the
        snapshot — discovery (and snapshot write) happens only on the FIRST
        event from a new child."""
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        agent = _build_agent(
            tmp_path,
            _build_transport(
                [
                    _text("first", source="child_1"),
                    _text("second", source="child_1"),
                    _text("third", source="child_1"),
                ]
            ),
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        # Discovery ran once (resolve called once by normalizer + once by sink).
        assert sink.resolve_calls == ["child_1", "child_1"]
        paths = ExternalPaths(tmp_path)
        assert paths.env_snapshot_for_session("child_1").exists()

    async def test_multiple_children_get_separate_snapshots(self, tmp_path: Path) -> None:
        await self._drive(
            tmp_path,
            [_text("from a", source="child_a"), _text("from b", source="child_b")],
        )

        paths = ExternalPaths(tmp_path)
        snap_a = json.loads(paths.env_snapshot_for_session("child_a").read_text(encoding="utf-8"))
        snap_b = json.loads(paths.env_snapshot_for_session("child_b").read_text(encoding="utf-8"))
        assert snap_a["MODEX_SESSION_ID"] == "mock_modex_child_a"
        assert snap_b["MODEX_SESSION_ID"] == "mock_modex_child_b"

    async def test_child_snapshot_includes_path(self, tmp_path: Path) -> None:
        """The child snapshot must include PATH (same as the main session
        snapshot) so modexctl can find the binary."""
        await self._drive(tmp_path, [_text("child text", source="child_1")])

        paths = ExternalPaths(tmp_path)
        snapshot = json.loads(paths.env_snapshot_for_session("child_1").read_text(encoding="utf-8"))
        assert "PATH" in snapshot

    async def test_no_child_snapshot_without_discovery_collaborators(self, tmp_path: Path) -> None:
        """When discovery collaborators are not configured, child events are
        dropped and NO snapshot file is written."""
        agent = _build_agent(tmp_path, _build_transport([_text("child text", source="child_1")]))

        await agent.run(_make_ctx(), _RecordingEmitter())

        paths = ExternalPaths(tmp_path)
        assert not paths.env_snapshot_for_session("child_1").exists()


# ---------------------------------------------------------------------------
# 8. Concurrent turn isolation — two sessions, no crossover
# ---------------------------------------------------------------------------


class TestConcurrentTurnIsolation:
    """Two sessions run concurrent turns. Per-turn routing state lives in
    the normalizer (one per turn), so session B's turn can never route
    children under session A's id."""

    async def test_concurrent_sessions_child_parent_correct(self, tmp_path: Path) -> None:
        sink_a = _MockSink()
        sink_b = _MockSink()
        child_emitters_a: dict[str, _RecordingChildEmitter] = {}
        child_emitters_b: dict[str, _RecordingChildEmitter] = {}

        def _build(
            transport: ScriptedTransport,
            sink: _MockSink,
            emitters: dict[str, _RecordingChildEmitter],
            session_id: str,
        ) -> ExternalAgent:
            return ExternalAgent(
                backend_provider=PoolScopedBackendProvider(transport),
                session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
                provider_kind=ProviderKind.OPENCODE,
                spec=_make_spec(tmp_path, session_id=session_id),
                base_env={"PATH": "/usr/bin"},
                child_discovery_sink=sink,
                session_registry=_MockRegistry(),
                child_emitter_factory=_make_child_emitter_factory(emitters),
            )

        agent_a = _build(
            _build_transport(
                [
                    _text("main A"),
                    _text("child A text", source="child_prov_A"),
                    _text("main A end"),
                ]
            ),
            sink_a,
            child_emitters_a,
            "pool1.agentA",
        )
        agent_b = _build(
            _build_transport(
                [
                    _text("main B"),
                    _text("child B text", source="child_prov_B"),
                    _text("main B end"),
                ]
            ),
            sink_b,
            child_emitters_b,
            "pool1.agentB",
        )

        ctx_a = _make_ctx("pool1.agentA")
        ctx_b = _make_ctx("pool1.agentB")

        await asyncio.gather(
            agent_a.run(ctx_a, _RecordingEmitter()),
            agent_b.run(ctx_b, _RecordingEmitter()),
        )

        assert sink_a.discovered == [("child_prov_A", "pool1.agentA")]
        assert sink_b.discovered == [("child_prov_B", "pool1.agentB")]
