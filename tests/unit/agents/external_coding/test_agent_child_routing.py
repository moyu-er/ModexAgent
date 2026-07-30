"""Unit tests for child session emission routing in ``ExternalCodingAgent``.

Covers the Todo 5 acceptance criteria:

1. **Happy path** — main text → main emitter; child text → child emitter;
   child tool → child emitter; main text → main emitter. Discovery sink
   called, mapping committed.
2. **Race condition** — first child emission triggers discovery
   synchronously in ``_handle_emission``; the same call routes to the
   newly created child emitter. No drop.
3. **Backward compat** — agent constructed without discovery
   collaborators → child emissions dropped with warning (no crash).
4. **Accumulator isolation** — child ``TOOL_USE`` → child accumulator
   updated; parent accumulator unchanged.
5. **Resume** — two turns with the same ``provider_child_sid`` →
   deterministic modex session id; ``on_child_discovered`` fires each
   turn (per-turn dicts are reinitialized).
6. **Emitter lifecycle** — after ``_run_turn``, child emitters cleared,
   pending tasks gathered.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from modex_agent.agents.external_coding.agent import (
    ExternalCodingAgent,
    StreamingProviderBackend,
)
from modex_agent.agents.external_coding.backend_provider import (
    PoolScopedBackendProvider,
)
from modex_agent.agents.external_coding.child_discovery import (
    ChildSessionDiscoverySink,
)
from modex_agent.agents.external_coding.events import ExternalCodingEvent
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.pi_parser import PiEventParser
from modex_agent.agents.external_coding.session_store import (
    LocalFileExternalSessionMapStore,
)
from modex_agent.agents.external_coding.types import (
    BackendResult,
    BackendStatus,
    Emission,
    ExecOptions,
    ExternalEnvSpec,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.core.types import MessageRole

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingEmitter(ContentEmitter[ExternalCodingEvent]):  # type: ignore[type-arg]
    """Main-session emitter capturing turn events, deltas, completes, errors."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas: list[str] = []
        self.turn_events: list[TurnEvent] = []
        self.completed: AgentResult | None = None
        self.errors: list[str] = []

    def wants_streaming(self) -> bool:
        return False

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_turn_event(self, event: TurnEvent) -> None:
        self.turn_events.append(event)

    async def emit_complete(self, result: AgentResult) -> None:
        self.completed = result

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)


class _RecordingChildEmitter(ContentEmitter[ExternalCodingEvent]):  # type: ignore[type-arg]
    """Child-session emitter — records the modex_sid it was created for."""

    def __init__(self, modex_sid: str) -> None:
        super().__init__()
        self.modex_sid = modex_sid
        self.turn_events: list[TurnEvent] = []
        self.errors: list[str] = []
        self.deltas: list[str] = []

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_turn_event(self, event: TurnEvent) -> None:
        self.turn_events.append(event)

    async def emit_complete(self, result: AgentResult) -> None:
        pass

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)


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


class _DirectEmissionAdapter(StreamingProviderBackend):
    """Test adapter that directly replays pre-constructed ``Emission`` objects.

    Unlike ``ScriptedStreamingAdapter`` (which parses step text through a
    parser), this adapter emits ``Emission`` records verbatim — including
    ``source_session_id`` — so child routing can be exercised without a
    provider-specific parser that knows about child sessions.
    """

    def __init__(self, emissions: list[Emission]) -> None:
        super().__init__()
        self._emissions = emissions
        self.recorded_opts: list[ExecOptions] = []
        self.recorded_envs: list[dict[str, str]] = []

    async def execute_streaming(
        self,
        opts: ExecOptions,
        env: dict[str, str],
        on_emission: Callable[[Emission], Awaitable[None]],
    ) -> BackendResult:
        self.recorded_opts.append(opts)
        self.recorded_envs.append(env)
        for emission in self._emissions:
            await on_emission(emission)
            # Yield so background discovery tasks get a chance to run.
            await asyncio.sleep(0)
        return BackendResult(status=BackendStatus.COMPLETED, session_id="prov-sess-1")


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


def _pool_provider(backend: StreamingProviderBackend) -> PoolScopedBackendProvider:
    return PoolScopedBackendProvider(backend)


def _make_child_emitter_factory(
    emitters: dict[str, _RecordingChildEmitter],
) -> Callable[[str], ContentEmitter[ExternalCodingEvent]]:
    def factory(modex_sid: str) -> ContentEmitter[ExternalCodingEvent]:
        emitter = _RecordingChildEmitter(modex_sid)
        emitters[modex_sid] = emitter
        return emitter

    return factory


def _text(text: str, source: str | None = None) -> Emission:
    return Emission(event=ExternalCodingEvent.TEXT_DELTA, text=text, source_session_id=source)


def _tool_use(call_id: str, tool_name: str = "bash", source: str | None = None) -> Emission:
    return Emission(
        event=ExternalCodingEvent.TOOL_USE,
        tool_name=tool_name,
        call_id=call_id,
        tool_input='{"cmd":"ls"}',
        source_session_id=source,
    )


def _tool_result(call_id: str, output: str = "ok", source: str | None = None) -> Emission:
    return Emission(
        event=ExternalCodingEvent.TOOL_RESULT,
        call_id=call_id,
        output=output,
        source_session_id=source,
    )


def _build_agent(
    tmp_path: Path,
    adapter: StreamingProviderBackend,
    *,
    sink: ChildSessionDiscoverySink | None = None,
    registry: SessionRegistry | None = None,
    emitter_factory: Callable[[str], ContentEmitter[ExternalCodingEvent]] | None = None,
) -> ExternalCodingAgent:
    return ExternalCodingAgent(
        backend_provider=_pool_provider(adapter),
        session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
        parser=PiEventParser(),
        provider_kind=ProviderKind.PI,
        spec=_make_spec(tmp_path),
        base_env={"PATH": "/usr/bin"},
        child_discovery_sink=sink,
        session_registry=registry,
        child_emitter_factory=emitter_factory,
    )


# ---------------------------------------------------------------------------
# 1. Happy path — main + child interleaved emissions routed correctly
# ---------------------------------------------------------------------------


class TestHappyPathRouting:
    async def test_main_and_child_emissions_routed_to_correct_emitters(
        self, tmp_path: Path
    ) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        emissions = [
            _text("main text 1"),
            _text("child text", source="child_1"),
            _tool_use("c-child", source="child_1"),
            _text("main text 2"),
        ]
        adapter = _DirectEmissionAdapter(emissions)
        agent = _build_agent(
            tmp_path,
            adapter,
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
        assert len(child.turn_events) == 2
        assert child.turn_events[0] == TurnTextEvent(text="child text")
        assert isinstance(child.turn_events[1], TurnToolCallEvent)
        assert child.turn_events[1].tool_name == "bash"

        # Discovery sink was called once for child_1 with the parent sid.
        assert sink.discovered == [("child_1", "pool1.agent1")]
        # resolve called once by agent (sync step 1) + once by mock
        # sink's on_child_discovered (async step 4).
        assert sink.resolve_calls == ["child_1", "child_1"]


# ---------------------------------------------------------------------------
# 2. Race condition — first child emission routed in the same call
# ---------------------------------------------------------------------------


class TestRaceConditionNoDrop:
    async def test_first_child_emission_not_dropped(self, tmp_path: Path) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        # Only one emission — a child text delta. If discovery were async,
        # this emission would be dropped before the child emitter exists.
        emissions = [_text("only child", source="child_x")]
        adapter = _DirectEmissionAdapter(emissions)
        agent = _build_agent(
            tmp_path,
            adapter,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        await agent.run(_make_ctx(), main_emitter)

        # The child emitter was created and received the text.
        assert "mock_modex_child_x" in child_emitters
        child = child_emitters["mock_modex_child_x"]
        assert len(child.turn_events) == 1
        assert child.turn_events[0] == TurnTextEvent(text="only child")

        # Main emitter received nothing from the child.
        assert main_emitter.turn_events == []

        # Discovery ran.
        assert sink.discovered == [("child_x", "pool1.agent1")]


# ---------------------------------------------------------------------------
# 3. Backward compat — no discovery collaborators → child dropped, no crash
# ---------------------------------------------------------------------------


class TestBackwardCompatNoCollaborators:
    async def test_child_emissions_dropped_gracefully(self, tmp_path: Path) -> None:
        emissions = [
            _text("main text"),
            _text("child text", source="child_1"),
            _text("more main"),
        ]
        adapter = _DirectEmissionAdapter(emissions)
        # No child_discovery_sink, no child_emitter_factory.
        agent = _build_agent(tmp_path, adapter)
        main_emitter = _RecordingEmitter()

        result = await agent.run(_make_ctx(), main_emitter)

        assert result.stop_reason == StopReason.COMPLETED

        # Main emitter received only the two main text deltas.
        main_texts = [e for e in main_emitter.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in main_texts] == ["main text", "more main"]

        # No child emitters were created.
        assert agent._child_emitters == {}


# ---------------------------------------------------------------------------
# 4. Accumulator isolation — child TOOL_USE does not leak to parent
# ---------------------------------------------------------------------------


class TestAccumulatorIsolation:
    async def test_child_tool_use_does_not_populate_parent_accumulator(
        self, tmp_path: Path
    ) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        emissions = [
            _tool_use("c-child", tool_name="grep", source="child_1"),
            _tool_use("c-main", tool_name="bash"),
            # TOOL_RESULT for the child call_id → must resolve from child accumulator.
            _tool_result("c-child", output="child output", source="child_1"),
            # TOOL_RESULT for the main call_id → must resolve from main accumulator.
            _tool_result("c-main", output="main output"),
        ]
        adapter = _DirectEmissionAdapter(emissions)
        agent = _build_agent(
            tmp_path,
            adapter,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        await agent.run(_make_ctx(), main_emitter)

        # Main emitter received the main TOOL_CALL + TOOL_RESULT.
        main_tool_calls = [e for e in main_emitter.turn_events if isinstance(e, TurnToolCallEvent)]
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
        child_tool_calls = [e for e in child.turn_events if isinstance(e, TurnToolCallEvent)]
        child_tool_results = [e for e in child.turn_events if isinstance(e, TurnToolResultEvent)]
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
        adapter_1 = _DirectEmissionAdapter([_text("turn1 child", source="child_1")])
        agent = _build_agent(
            tmp_path,
            adapter_1,
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
        # Per-turn dicts are reinitialized, so discovery runs again.
        adapter_2 = _DirectEmissionAdapter([_text("turn2 child", source="child_1")])
        agent._backend_provider = _pool_provider(adapter_2)
        await agent.run(_make_ctx(), _RecordingEmitter())

        # on_child_discovered fired again (per-turn dicts reinitialized).
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
# 6. Emitter lifecycle — cleared after _run_turn, pending tasks gathered
# ---------------------------------------------------------------------------


class TestEmitterLifecycle:
    async def test_child_state_cleared_after_turn(self, tmp_path: Path) -> None:
        sink = _MockSink()
        child_emitters: dict[str, _RecordingChildEmitter] = {}
        emissions = [_text("child text", source="child_1")]
        adapter = _DirectEmissionAdapter(emissions)
        agent = _build_agent(
            tmp_path,
            adapter,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )

        await agent.run(_make_ctx(), _RecordingEmitter())

        # All per-turn child containers are cleared in the finally block.
        assert agent._child_emitters == {}
        assert agent._child_sid_to_modex_sid == {}
        assert agent._child_accumulators == {}
        assert agent._pending_child_tasks == set()

    async def test_pending_child_tasks_gathered_before_release(self, tmp_path: Path) -> None:
        # A sink whose on_child_discovered blocks until released — proves
        # the finally block awaits it before returning.
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
        emissions = [_text("child text", source="child_1")]
        adapter = _DirectEmissionAdapter(emissions)
        agent = _build_agent(
            tmp_path,
            adapter,
            sink=sink,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )

        # Run the turn in a task so we can control the blocking sink.
        run_task = asyncio.create_task(agent.run(_make_ctx(), _RecordingEmitter()))

        # Let the adapter replay emissions and create the discovery task.
        # The finally block will block on gather(pending_child_tasks).
        await asyncio.sleep(0.1)

        # The turn hasn't completed because the discovery task is blocked.
        assert not run_task.done()

        # Release the sink — the gather completes, the turn finishes.
        release_event.set()
        result = await asyncio.wait_for(run_task, timeout=2.0)

        assert result.stop_reason == StopReason.COMPLETED
        assert sink.discovered == [("child_1", "pool1.agent1")]

        # Per-turn state cleared.
        assert agent._pending_child_tasks == set()
