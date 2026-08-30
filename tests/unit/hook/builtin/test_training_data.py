"""Tests for TrainingDataHook — L1 training-relevance tagging at turn end."""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook import HookErrorPolicy, HookPayload, HookPoint, HookRunner, HookSpec
from modex_agent.hook.abc import FinallyGraphHook
from modex_agent.hook.builtin.training_data import TRAINING_RELEVANT_ATTR, TrainingDataHook
from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import (
    AgentKind,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.scoring import TrajectoryMetrics
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.session_state import MetricCounters, TraceSessionState
from modex_agent.trace.store import SpanModel, SpanStatus

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

_TRACE_ID = "trace-abc-123"
_SESSION_ID = "s1.bot"
_AGENT_NAME = "bot"


class _RecordingOtelStore(OtelSpanTraceStore):
    """In-memory OtelSpanTraceStore double: records every save_span."""

    def __init__(self) -> None:
        # Intentionally do NOT call super().__init__ — we override all I/O.
        self.saved: list[SpanModel] = []

    async def save_span(self, span: SpanModel) -> None:
        self.saved.append(span)


class _RaisingOtelStore(_RecordingOtelStore):
    async def save_span(self, span: SpanModel) -> None:
        raise OSError("disk full")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _identity(turn_id: str = "t1") -> TurnIdentity:
    return TurnIdentity(
        agent_id="bot",
        session=SessionInfo.from_str(_SESSION_ID),
        turn_id=turn_id,
    )


def _react_state(
    *,
    iteration: int = 1,
    turn_id: str = "t1",
    phase: TurnPhase = TurnPhase.COMPLETED,
    trace_id: str = _TRACE_ID,
) -> ReActTurnState:
    state = ReActTurnState(
        identity=_identity(turn_id),
        agent_kind=AgentKind.REACT,
        phase=phase,
        current_node=ReActNode.END,
        iteration=iteration,
    )
    state.custom[TurnCustomKey.TRACE_ID] = trace_id
    return state


def _ctx(
    state: ReActTurnState,
    trace_store: OtelSpanTraceStore | None,
) -> AgentContext:
    services = AgentRuntimeServices(trace_store=trace_store)
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(_SESSION_ID),
        max_iterations=5,
        identity=state.identity,
        runtime=runtime,
    )


def _result(*, stop_reason: StopReason = StopReason.COMPLETED) -> AgentResult:
    return AgentResult(content="ok", stop_reason=stop_reason)


def _stash_metrics(
    state: ReActTurnState,
    *,
    input_tokens: int,
    output_tokens: int,
) -> None:
    metrics = (
        MetricCounters()
        .to_metrics()
        .model_copy(
            update={
                "total_input_tokens": input_tokens,
                "total_output_tokens": output_tokens,
            }
        )
    )
    state.custom[TurnCustomKey.TRAJECTORY_METRICS] = metrics


def _runner(hook: TrainingDataHook) -> HookRunner:
    return HookRunner([HookSpec(hook=hook, on_error=HookErrorPolicy.LOG)])


async def _fire(
    ctx: AgentContext,
    hook: TrainingDataHook,
    result: AgentResult | None,
) -> None:
    payload = HookPayload(data={"result": result})
    await _runner(hook).dispatch(HookPoint.FINALLY_GRAPH, ctx, payload)


def _training_span(store: _RecordingOtelStore) -> SpanModel:
    """Return the single training_tag span saved by the hook."""
    tag_spans = [s for s in store.saved if s.name == "training_tag"]
    assert len(tag_spans) == 1, f"expected 1 training_tag span, got {len(tag_spans)}"
    return tag_spans[0]


# ---------------------------------------------------------------------------
# L1 rule 4 (default): successful turn within thresholds → True
# ---------------------------------------------------------------------------


async def test_successful_turn_within_thresholds_marks_relevant_true() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=3)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


async def test_zero_iterations_marks_relevant_true() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=0)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


# ---------------------------------------------------------------------------
# L1 rule 1: stop_reason failure/cancel → False
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stop_reason",
    [
        StopReason.ERROR,
        StopReason.TURN_CANCELLED,
        StopReason.CANCELLED,
        StopReason.TIMEOUT,
        StopReason.LOOP_DETECTED,
        StopReason.COMMAND_INTERCEPTED,
    ],
)
async def test_failed_or_cancelled_stop_reason_marks_relevant_false(
    stop_reason: StopReason,
) -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(), _result(stop_reason=stop_reason))

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is False


async def test_max_iterations_stop_reason_stays_relevant_when_under_training_budget() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=5)
    ctx = _ctx(state, store)

    await _fire(
        ctx,
        TrainingDataHook(max_iterations=20, max_tokens=100_000),
        _result(stop_reason=StopReason.MAX_ITERATIONS),
    )

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


async def test_suspend_result_none_emits_no_tag() -> None:
    """Supersedes test_none_result_marks_relevant_true (None=eligible) — that
    behavior WAS this round's defect: ``result=None`` is the GraphInterrupt
    approval-suspend payload (agent.py: ``except GraphInterrupt: result =
    None; raise`` — every terminal path assigns a concrete AgentResult
    first), and a suspend-time ``relevant=true`` tag poisons the trajectory
    because the exporter accepts on ANY true tag. Suspend must emit nothing."""
    store = _RecordingOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(), result=None)

    assert store.saved == []


async def test_suspend_then_terminal_failure_real_sequence_one_false_tag() -> None:
    """Real suspend→resume sequence through a real HookRunner (factory
    order: RootSpanHook then TrainingDataHook, priority 0). Suspend
    (``result=None``) must emit ZERO tags; the terminal error finalize must
    emit exactly ONE false tag — never a suspend-time true tag that the
    exporter's any-true acceptance would make permanent."""
    store = _RecordingOtelStore()
    session = TraceSessionState()
    root = RootSpanHook(session=session, store=store)
    training = TrainingDataHook(max_iterations=20, max_tokens=100_000)
    runner = HookRunner(
        [
            HookSpec(hook=root, on_error=HookErrorPolicy.LOG),
            HookSpec(hook=training, on_error=HookErrorPolicy.LOG),
        ]
    )

    state = _react_state(iteration=1)
    ctx = _ctx(state, store)
    await root.start_node_turn(ctx)
    trace_id = str(state.custom[TurnCustomKey.TRACE_ID])
    root_span_id = str(state.custom[TurnCustomKey.ROOT_SPAN_ID])
    session.accumulate_span(
        trace_id,
        root_span_id,
        SpanModel(
            trace_id=trace_id,
            span_id="chat-pre-suspend",
            name=SpanName.CHAT.value,
            kind=SpanKind.CLIENT.value,
            start_time=1000.0,
            end_time=1001.0,
            attributes={GenAiAttr.USAGE_INPUT_TOKENS: 100},
            status=SpanStatus(code=SpanStatusCode.OK),
        ),
    )

    await runner.dispatch(HookPoint.FINALLY_GRAPH, ctx, HookPayload(data={"result": None}))

    assert [s for s in store.saved if s.name == "training_tag"] == []
    assert [s for s in store.saved if s.name == "invoke_agent"] == []

    state2 = _react_state(iteration=2)
    ctx2 = _ctx(state2, store)
    session.accumulate_span(
        trace_id,
        root_span_id,
        SpanModel(
            trace_id=trace_id,
            span_id="chat-post-resume",
            name=SpanName.CHAT.value,
            kind=SpanKind.CLIENT.value,
            start_time=2000.0,
            end_time=2001.0,
            attributes={GenAiAttr.USAGE_INPUT_TOKENS: 250},
            status=SpanStatus(code=SpanStatusCode.OK),
        ),
    )

    await runner.dispatch(
        HookPoint.FINALLY_GRAPH,
        ctx2,
        HookPayload(data={"result": _result(stop_reason=StopReason.ERROR)}),
    )

    tag_spans = [s for s in store.saved if s.name == "training_tag"]
    assert len(tag_spans) == 1
    assert tag_spans[0].attributes[TRAINING_RELEVANT_ATTR] is False
    stashed = state2.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert isinstance(stashed, TrajectoryMetrics)
    assert stashed.llm_call_count == 2  # whole-turn metrics survived the suspend
    assert trace_id not in session._metric_counters


# ---------------------------------------------------------------------------
# L1 rule 2: iteration count exceeds max_iterations → False
# ---------------------------------------------------------------------------


async def test_iteration_exceeding_max_iterations_marks_relevant_false() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=25)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is False


async def test_iteration_equal_to_max_iterations_marks_relevant_true() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=20)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


# ---------------------------------------------------------------------------
# L1 rule 3: total token usage exceeds max_tokens → False
# ---------------------------------------------------------------------------


async def test_tokens_exceeding_max_tokens_marks_relevant_false() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=2)
    _stash_metrics(state, input_tokens=60_000, output_tokens=50_000)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is False


async def test_tokens_equal_to_max_tokens_marks_relevant_true() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=1)
    _stash_metrics(state, input_tokens=60_000, output_tokens=40_000)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


async def test_missing_stash_counts_zero_tokens() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=10), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


async def test_token_gate_reads_root_hook_stash_in_factory_order() -> None:
    """Convergence gate: RootSpanHook.finally_graph runs first (registration
    order, priority 0) and CLEARS the counters bucket, so the training token
    gate must read the stashed TrajectoryMetrics — a store read would see
    nothing and wrongly mark the turn relevant."""
    store = _RecordingOtelStore()
    session = TraceSessionState()
    root = RootSpanHook(session=session, store=store)
    training = TrainingDataHook(max_iterations=20, max_tokens=100_000)
    runner = HookRunner(
        [
            HookSpec(hook=root, on_error=HookErrorPolicy.LOG),
            HookSpec(hook=training, on_error=HookErrorPolicy.LOG),
        ]
    )

    state = _react_state(iteration=2)
    ctx = _ctx(state, store)
    await root.start_node_turn(ctx)

    trace_id = str(state.custom[TurnCustomKey.TRACE_ID])
    root_span_id = str(state.custom[TurnCustomKey.ROOT_SPAN_ID])
    for index, (input_tokens, output_tokens) in enumerate([(60_000, 0), (0, 50_000)]):
        session.accumulate_span(
            trace_id,
            root_span_id,
            SpanModel(
                trace_id=trace_id,
                span_id=f"chat-{index}",
                name=SpanName.CHAT.value,
                kind=SpanKind.CLIENT.value,
                start_time=1000.0,
                end_time=1001.0,
                attributes={
                    GenAiAttr.USAGE_INPUT_TOKENS: input_tokens,
                    GenAiAttr.USAGE_OUTPUT_TOKENS: output_tokens,
                },
                status=SpanStatus(code=SpanStatusCode.OK),
            ),
        )

    await runner.dispatch(HookPoint.FINALLY_GRAPH, ctx, HookPayload(data={"result": _result()}))

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is False
    stashed = state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert isinstance(stashed, TrajectoryMetrics)
    assert stashed.total_input_tokens + stashed.total_output_tokens == 110_000


# ---------------------------------------------------------------------------
# No-op guards
# ---------------------------------------------------------------------------


async def test_noop_when_state_not_react() -> None:
    from modex_agent.runtime.models import TurnStateBase

    store = _RecordingOtelStore()
    plain = TurnStateBase(
        identity=_identity(),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.COMPLETED,
    )
    services = AgentRuntimeServices(trace_store=store)
    runtime = AgentRuntime(services=services, state=plain)
    ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(_SESSION_ID),
        max_iterations=5,
        identity=plain.identity,
        runtime=runtime,
    )

    await _fire(ctx, TrainingDataHook(), _result())

    assert store.saved == []


async def test_noop_when_trace_store_none() -> None:
    state = _react_state()
    ctx = _ctx(state, trace_store=None)

    await _fire(ctx, TrainingDataHook(), _result())


async def test_noop_when_runtime_none() -> None:
    state = _react_state()
    ctx = _ctx(state, trace_store=None)
    ctx.runtime = None

    await _fire(ctx, TrainingDataHook(), _result())


async def test_save_failure_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _RaisingOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    with caplog.at_level("WARNING", logger="modex_agent.hook.builtin.training_data"):
        await _fire(ctx, TrainingDataHook(), _result())

    assert store.saved == []
    assert any("TrainingDataHook failed to save" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Saved span shape
# ---------------------------------------------------------------------------


async def test_saved_span_uses_training_tag_name() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(), _result())

    span = _training_span(store)
    assert span.name == "training_tag"
    assert span.trace_id == _TRACE_ID
    assert span.attributes[GenAiAttr.CONVERSATION_ID] == _SESSION_ID
    assert span.attributes[GenAiAttr.AGENT_NAME] == _AGENT_NAME
    assert span.status.code == SpanStatusCode.OK


# ---------------------------------------------------------------------------
# Deployment wiring (the observability-driven registration moved from the
# retired DefaultAgentFactory injection to the deployment's shared runner —
# bot wiring; the hook itself is unchanged)
# ---------------------------------------------------------------------------


def _deployment_hooks(obs: ObservabilityConfig) -> list[HookSpec]:
    """The bot wiring's construction (resources.py mirror): the training
    hook joins the shared runner iff ``training_relevant``."""
    if obs.training_relevant:
        return [
            HookSpec(
                hook=TrainingDataHook(
                    max_iterations=obs.training_max_iterations,
                    max_tokens=obs.training_max_tokens,
                )
            )
        ]
    return []


async def test_deployment_runner_carries_training_data_hook_when_enabled() -> None:
    runner = HookRunner(
        _deployment_hooks(
            ObservabilityConfig(
                training_relevant=True,
                training_max_iterations=15,
                training_max_tokens=50_000,
            )
        )
    )
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert TrainingDataHook in kinds


async def test_deployment_runner_no_training_data_hook_when_disabled() -> None:
    runner = HookRunner(_deployment_hooks(ObservabilityConfig(training_relevant=False)))
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert TrainingDataHook not in kinds


async def test_deployment_disabled_by_default() -> None:
    runner = HookRunner(_deployment_hooks(ObservabilityConfig()))
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert TrainingDataHook not in kinds


async def test_deployment_passes_max_iterations_and_max_tokens_to_hook() -> None:
    runner = HookRunner(
        _deployment_hooks(
            ObservabilityConfig(
                training_relevant=True,
                training_max_iterations=7,
                training_max_tokens=42_000,
            )
        )
    )
    training_hooks = [s.hook for s in runner.hook_specs if isinstance(s.hook, TrainingDataHook)]
    assert len(training_hooks) == 1
    hook = training_hooks[0]
    assert hook._max_iterations == 7  # noqa: SLF001
    assert hook._max_tokens == 42_000  # noqa: SLF001


# ---------------------------------------------------------------------------
# isinstance dispatch contract
# ---------------------------------------------------------------------------


def test_training_data_hook_is_finally_graph_hook() -> None:
    hook = TrainingDataHook()
    assert isinstance(hook, FinallyGraphHook)
    assert hook.name == "training_data"


def test_default_thresholds_match_observability_config() -> None:
    hook = TrainingDataHook()
    assert hook._max_iterations == 20
    assert hook._max_tokens == 100_000


# keep the Any import meaningful for static analyzers
_ANNOT: Any = None
