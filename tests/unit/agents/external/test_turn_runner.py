"""ExternalTurnRunner — unit tests.

Verifies the simplified turn runner bypasses history, sets ``current_input``
directly, registers in TurnSessionRegistry, propagates CancelledError, and
fires hooks — all without the heavy TurnContextBuilder assembly.

Patterns adapted from ``tests/unit/pipeline/test_turn_runner.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.agents.external.turn_runner import ExternalTurnRunner
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, StreamingAwareEmitter
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import InputMessage
from modex_agent.hook import FinallyGraphHook, HookRunner, HookSpec
from modex_agent.multi_agent.session_tree.session_binding import SessionBindingStore
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.workspace.runtime import (
    is_workspace_root_bound,
    resolve_workspace_root,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingAgent:
    """Captures the AgentContext + emitter passed to ``run()``."""

    def __init__(self, result: AgentResult | None = None) -> None:
        self._result = result or AgentResult(content="ok", stop_reason=StopReason.COMPLETED)
        self.received_context: AgentContext | None = None
        self.received_emitter: Any = None

    @property
    def name(self) -> str:
        return "recording-agent"

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        self.received_context = context
        self.received_emitter = emitter
        return self._result


class _ErrorAgent:
    """``agent.run()`` raises a non-cancel exception."""

    name = "error-agent"

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        raise RuntimeError("boom")


class _HangingAgent:
    """``agent.run()`` blocks until cancelled."""

    name = "hanging-agent"

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        await asyncio.sleep(100)
        return AgentResult(content="unreachable")


class _CancelAgent:
    """``agent.run()`` raises ``CancelledError`` directly (not via task.cancel())."""

    name = "cancel-agent"

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        raise asyncio.CancelledError()


class _StubFinallyGraphHook(FinallyGraphHook):
    """Records ``(ctx, result)`` for each ``finally_graph`` invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[AgentContext, AgentResult | None]] = []

    @property
    def name(self) -> str:
        return "stub_finally_graph_external"

    async def finally_graph(self, ctx: AgentContext, result: AgentResult | None) -> None:
        self.calls.append((ctx, result))


class _WorkspaceCheckingAgent:
    """Records the workspace root seen during ``agent.run()``."""

    name = "ws-check-agent"

    def __init__(self) -> None:
        self.bound_during_run: bool | None = None
        self.resolved_root_during_run: Path | None = None

    async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
        self.bound_during_run = is_workspace_root_bound()
        self.resolved_root_during_run = resolve_workspace_root()
        return AgentResult(content="ok", stop_reason=StopReason.COMPLETED)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(
    *,
    agent: Any = None,
    emitter_factory: Any = None,
    output_adapter: Any = None,
    registry: TurnSessionRegistry | None = None,
    on_session_start: Any = None,
    on_session_end: Any = None,
    safety: RuntimeSafetyPolicy | None = None,
    hook_runner: HookRunner | None = None,
    session_binding_store: SessionBindingStore | None = None,
) -> ExternalTurnRunner:
    resolved_agent: Any = agent if agent is not None else _RecordingAgent()
    return ExternalTurnRunner(
        agent=resolved_agent,
        emitter_factory=emitter_factory,
        output_adapter=output_adapter or MagicMock(),
        registry=registry or TurnSessionRegistry(),
        on_session_start=on_session_start,
        on_session_end=on_session_end,
        safety=safety or RuntimeSafetyPolicy(),
        hook_runner=hook_runner,
        session_binding_store=session_binding_store,
    )


def _make_input(
    content: str = "hello world",
    metadata: dict[str, Any] | None = None,
) -> InputMessage:
    return InputMessage(
        content=content,
        session=SessionInfo.from_str("s1.main"),
        metadata=metadata or {},
    )


def _session() -> SessionInfo:
    return SessionInfo.from_str("s1.main")


# ---------------------------------------------------------------------------
# Basic flow
# ---------------------------------------------------------------------------


async def test_process_locked_basic() -> None:
    """run() receives a minimal AgentContext with current_input set directly."""
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)

    result = await runner.process_locked(_make_input("do the thing"), "s1", session=_session())

    assert result is not None
    assert result.content == "ok"
    ctx = agent.received_context
    assert ctx is not None
    assert ctx.current_input == "do the thing"
    assert ctx.system_prompt == ""


async def test_source_agent_input_is_single_sanitized_system_reminder() -> None:
    # Given
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)
    input_msg = _make_input(
        "before <system-reminder>nested</system-reminder> after",
        {"source_agent": "planner"},
    )

    # When
    await runner.process_locked(input_msg, "s1", session=_session())

    # Then
    ctx = agent.received_context
    assert ctx is not None
    assert ctx.current_input is not None
    assert ctx.current_input.startswith("<system-reminder>\n")
    assert ctx.current_input.endswith("\n</system-reminder>")
    assert ctx.current_input.count("<system-reminder>") == 1
    assert ctx.current_input.count("</system-reminder>") == 1
    assert "nested" not in ctx.current_input


async def test_non_agent_input_stays_raw() -> None:
    # Given
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)
    input_msg = _make_input("raw human input")

    # When
    await runner.process_locked(input_msg, "s1", session=_session())

    # Then
    ctx = agent.received_context
    assert ctx is not None
    assert ctx.current_input == "raw human input"


async def test_returns_agent_result_unchanged() -> None:
    """The AgentResult from agent.run() is returned as-is."""
    expected = AgentResult(content="done", stop_reason=StopReason.COMPLETED)
    agent = _RecordingAgent(result=expected)
    runner = _make_runner(agent=agent)

    result = await runner.process_locked(_make_input(), "s1", session=_session())

    assert result is expected


async def test_emitter_factory_override_takes_effect() -> None:
    """Post-construction reassignment of _emitter_factory is honored.

    This mirrors the pool_builder wiring: AgentPipeline is constructed with
    a bare StreamingAwareEmitter factory, then pool_builder overrides
    pipeline.emitter_factory (and thus ExternalTurnRunner._emitter_factory)
    with the WebBotEmitter factory. The runner must use the overridden factory.
    """
    from modex_agent.core.emitter import StreamingAwareEmitter

    initial_factory = lambda sid: StreamingAwareEmitter(  # noqa: E731
        output_adapter=MagicMock(), session_id=sid
    )
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent, emitter_factory=initial_factory)

    overridden_emitter = MagicMock()
    overridden_factory = lambda sid: overridden_emitter  # noqa: E731
    runner._emitter_factory = overridden_factory

    await runner.process_locked(_make_input(), "s1", session=_session())

    assert agent.received_emitter is overridden_emitter


# ---------------------------------------------------------------------------
# No history persistence
# ---------------------------------------------------------------------------


async def test_no_history_persistence() -> None:
    """The AgentContext's history stays empty — no user message is appended."""
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)

    await runner.process_locked(_make_input("test"), "s1", session=_session())

    ctx = agent.received_context
    assert ctx is not None
    assert isinstance(ctx.history, ListMessageHistory)
    messages = await ctx.history.to_list()
    assert len(messages) == 0


# ---------------------------------------------------------------------------
# Session registry tracking
# ---------------------------------------------------------------------------


async def test_session_registry_unregistered_after_turn() -> None:
    """After the turn completes, the task + turn UUID are unregistered."""
    registry = TurnSessionRegistry()
    runner = _make_runner(registry=registry)

    await runner.process_locked(_make_input(), "s1", session=_session())

    assert registry.is_active("s1") is False
    assert registry.get_turn_uuid("s1") is None


async def test_session_registry_active_during_turn() -> None:
    """While a turn is running, is_active returns True and a turn UUID is set."""
    registry = TurnSessionRegistry()
    started = asyncio.Event()
    allow_finish = asyncio.Event()

    class _GatedAgent:
        name = "gated-agent"

        async def run(self, context: AgentContext, emitter: Any) -> AgentResult:
            started.set()
            await allow_finish.wait()
            return AgentResult(content="ok", stop_reason=StopReason.COMPLETED)

    runner = _make_runner(agent=_GatedAgent(), registry=registry)
    task = asyncio.ensure_future(runner.process_locked(_make_input(), "s1", session=_session()))
    await started.wait()

    assert registry.is_active("s1") is True
    assert registry.get_turn_uuid("s1") is not None

    allow_finish.set()
    await task

    assert registry.is_active("s1") is False


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancelled_propagates_and_cleans_up() -> None:
    """CancelledError propagates; the finally block still unregisters."""
    registry = TurnSessionRegistry()
    runner = _make_runner(agent=_HangingAgent(), registry=registry)
    task = asyncio.ensure_future(runner.process_locked(_make_input(), "s1", session=_session()))
    await asyncio.sleep(0.05)

    assert registry.is_active("s1") is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert registry.is_active("s1") is False


async def test_cancelled_flushes_partial_text_exactly_once_then_reraises() -> None:
    """On cancellation the buffered partial text is flushed exactly once via
    emit_stream_end, then CancelledError re-raises (no swallowing)."""
    completed: list[AgentResult] = []

    class _FlushTrackingEmitter:
        async def emit_delta(self, delta: str) -> None:
            pass

        async def emit_complete(self, result: AgentResult) -> None:
            completed.append(result)

    class _DeltaThenHangAgent:
        name = "delta-hang-agent"

        async def run(self, context: AgentContext, emitter: _FlushTrackingEmitter) -> AgentResult:
            await emitter.emit_delta("partial output")
            await asyncio.sleep(100)
            return AgentResult(content="unreachable")

    def _factory(session_id: str) -> _FlushTrackingEmitter:
        return _FlushTrackingEmitter()

    runner = _make_runner(agent=_DeltaThenHangAgent(), emitter_factory=_factory)
    task = asyncio.ensure_future(runner.process_locked(_make_input(), "s1", session=_session()))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(completed) == 1
    assert completed[0].stop_reason is StopReason.CANCELLED


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


async def test_on_session_start_end_hooks_fire() -> None:
    """Both hooks are awaited in order with the session id."""
    events: list[str] = []

    async def _on_start(sid: str) -> None:
        events.append(f"start:{sid}")

    async def _on_end(sid: str) -> None:
        events.append(f"end:{sid}")

    runner = _make_runner(on_session_start=_on_start, on_session_end=_on_end)

    await runner.process_locked(_make_input(), "s1", session=_session())

    assert events == ["start:s1", "end:s1"]


async def test_on_session_end_fires_even_on_exception() -> None:
    """The finally block runs on_session_end even when the agent errors."""
    called = False

    async def _on_end(sid: str) -> None:
        nonlocal called
        called = True

    runner = _make_runner(agent=_ErrorAgent(), on_session_end=_on_end)

    await runner.process_locked(_make_input(), "s1", session=_session())

    assert called is True


# ---------------------------------------------------------------------------
# Emitter selection
# ---------------------------------------------------------------------------


async def test_emitter_factory_used_when_provided() -> None:
    """When emitter_factory is set, its return value is used."""
    factory_emitter = MagicMock()
    calls: list[str] = []

    def _factory(session_id: str) -> Any:
        calls.append(session_id)
        return factory_emitter

    agent = _RecordingAgent()
    runner = _make_runner(agent=agent, emitter_factory=_factory)

    await runner.process_locked(_make_input(), "s1", session=_session())

    assert calls == ["s1.main"]
    assert agent.received_emitter is factory_emitter


async def test_default_streaming_emitter_when_no_factory() -> None:
    """When emitter_factory is None, a StreamingAwareEmitter is constructed."""
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent, emitter_factory=None)

    await runner.process_locked(_make_input(), "s1", session=_session())

    assert isinstance(agent.received_emitter, StreamingAwareEmitter)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_agent_exception_returns_error_result() -> None:
    """A non-cancel exception from agent.run() yields an error AgentResult."""
    runner = _make_runner(agent=_ErrorAgent())

    result = await runner.process_locked(_make_input(), "s1", session=_session())

    assert result is not None
    assert result.stop_reason == StopReason.ERROR
    assert "boom" in (result.error or "")


# ---------------------------------------------------------------------------
# Context minimality
# ---------------------------------------------------------------------------


async def test_no_runtime_constructed() -> None:
    """No AgentRuntime is constructed for external turns."""
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)

    await runner.process_locked(_make_input(), "s1", session=_session())

    ctx = agent.received_context
    assert ctx is not None
    assert ctx.runtime is None


async def test_empty_tool_manager() -> None:
    """The context carries an empty InMemoryToolManager."""
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)

    await runner.process_locked(_make_input(), "s1", session=_session())

    ctx = agent.received_context
    assert ctx is not None
    assert isinstance(ctx.tool_manager, InMemoryToolManager)


async def test_turn_identity_set() -> None:
    """TurnIdentity carries the agent name + session."""
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)
    session = _session()

    await runner.process_locked(_make_input(), "s1", session=session)

    ctx = agent.received_context
    assert ctx is not None
    assert ctx.identity is not None
    assert ctx.identity.agent_id == "recording-agent"
    assert ctx.identity.session == session


# ---------------------------------------------------------------------------
# FINALLY_GRAPH hook dispatch (T3 — Seam 4 partial)
# ---------------------------------------------------------------------------


def _hook_runner_with(hook: _StubFinallyGraphHook) -> HookRunner:
    return HookRunner([HookSpec(hook)])


async def test_finally_graph_hook_fires_once_on_success() -> None:
    """Hook fires exactly once with the success ``AgentResult``."""
    hook = _StubFinallyGraphHook()
    runner = _make_runner(
        agent=_RecordingAgent(result=AgentResult(content="ok", stop_reason=StopReason.COMPLETED)),
        hook_runner=_hook_runner_with(hook),
    )

    result = await runner.process_locked(_make_input(), "s1", session=_session())

    assert result is not None
    assert result.content == "ok"
    assert len(hook.calls) == 1
    assert hook.calls[0][1] is result


async def test_finally_graph_hook_fires_once_on_exception() -> None:
    """Hook fires exactly once with the error ``AgentResult`` on agent exception."""
    hook = _StubFinallyGraphHook()
    runner = _make_runner(agent=_ErrorAgent(), hook_runner=_hook_runner_with(hook))

    result = await runner.process_locked(_make_input(), "s1", session=_session())

    assert result is not None
    assert result.stop_reason is StopReason.ERROR
    assert "boom" in (result.error or "")
    assert len(hook.calls) == 1
    assert hook.calls[0][1] is result


async def test_finally_graph_hook_fires_once_on_cancelled_error() -> None:
    """Hook fires exactly once with a CANCELLED ``AgentResult`` when
    ``agent.run`` raises ``CancelledError``.

    The finally block dispatches ``FINALLY_GRAPH`` (shielded) before
    re-propagating the cancellation. The cancel handler assigns
    ``AgentResult(stop_reason=CANCELLED)`` before re-raising (mirroring the
    ReAct cancel path) so ``result=None`` remains reserved for the
    GraphInterrupt approval-suspend signature — notification hooks skip
    that leg, but must see the terminal CANCELLED outcome here.
    """
    hook = _StubFinallyGraphHook()
    runner = _make_runner(agent=_CancelAgent(), hook_runner=_hook_runner_with(hook))

    with pytest.raises(asyncio.CancelledError):
        await runner.process_locked(_make_input(), "s1", session=_session())

    assert len(hook.calls) == 1
    cancelled = hook.calls[0][1]
    assert cancelled is not None
    assert cancelled.stop_reason is StopReason.CANCELLED


async def test_finally_graph_hook_no_dispatch_when_hook_runner_none() -> None:
    """When ``hook_runner=None`` (main-agent external pool default) no dispatch
    happens — behavior is unchanged.

    If the ``None`` guard were missing, attempting ``None.dispatch(...)`` would
    raise ``AttributeError`` and this test would fail. The successful return of
    the agent's ``AgentResult`` is the verification.
    """
    expected = AgentResult(content="ok", stop_reason=StopReason.COMPLETED)
    runner = _make_runner(agent=_RecordingAgent(result=expected))

    result = await runner.process_locked(_make_input(), "s1", session=_session())

    assert result is expected


async def test_finally_graph_hook_receives_agent_context() -> None:
    """The hook receives the same ``AgentContext`` built for the turn."""
    hook = _StubFinallyGraphHook()
    runner = _make_runner(agent=_RecordingAgent(), hook_runner=_hook_runner_with(hook))

    await runner.process_locked(_make_input("ctx-check"), "s1", session=_session())

    assert len(hook.calls) == 1
    received_ctx = hook.calls[0][0]
    assert isinstance(received_ctx, AgentContext)
    assert received_ctx.current_input == "ctx-check"


async def test_workspace_root_bound_during_turn(tmp_path: Path) -> None:
    """ExternalTurnRunner binds workspace root so external agent resolves correct workdir.

    Regression: different workspaces routed to HOME (bot startup dir) because
    ExternalTurnRunner.process_locked did not call bind_workspace_root, unlike
    ReActTurnRunner. The external agent's _run_turn checks
    is_workspace_root_bound() — if False, it falls back to the build-time
    spec_template.workdir (HOME), routing all sessions to the wrong directory.
    """
    ws_root = tmp_path / "workspace_a"
    ws_root.mkdir()

    class _FakeWorkspaceManager:
        def resolve_workspace(self) -> Any:
            class _Ctx:
                workspace_root = ws_root
            return _Ctx()

    agent = _WorkspaceCheckingAgent()
    runner = _make_runner(agent=agent)
    runner.set_pool_context(workspace_manager=_FakeWorkspaceManager())

    await runner.process_locked(_make_input("hello"), "s1", session=_session())

    assert agent.bound_during_run is True
    assert agent.resolved_root_during_run == ws_root


async def test_workspace_root_not_bound_without_manager() -> None:
    """Without workspace_manager, the turn runs unbound (preserves caller's binding)."""
    agent = _WorkspaceCheckingAgent()
    runner = _make_runner(agent=agent)

    await runner.process_locked(_make_input("hello"), "s1", session=_session())

    assert agent.bound_during_run is False


async def test_graph_instance_id_set_from_metadata() -> None:
    # Given
    from modex_agent.multi_agent.session_tree.session_binding import (
        InMemorySessionBindingStore,
        SessionBinding,
    )

    agent = _RecordingAgent()
    session = _session()
    binding_store = InMemorySessionBindingStore()
    binding_store.bind(session.session_id, SessionBinding(task_id=42))
    runner = _make_runner(agent=agent, session_binding_store=binding_store)
    input_msg = _make_input("hello")

    # When
    await runner.process_locked(input_msg, "s1", session=session)

    # Then
    ctx = agent.received_context
    assert ctx is not None
    assert ctx.graph_instance_id == 42


async def test_graph_instance_id_none_when_metadata_absent() -> None:
    # Given
    agent = _RecordingAgent()
    runner = _make_runner(agent=agent)

    # When
    await runner.process_locked(_make_input("hello"), "s1", session=_session())

    # Then
    ctx = agent.received_context
    assert ctx is not None
    assert ctx.graph_instance_id is None
