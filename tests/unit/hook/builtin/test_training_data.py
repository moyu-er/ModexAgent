"""Tests for TrainingDataHook — L1 training-relevance tagging at turn end."""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import ExecutionStrategyKind, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook import HookErrorPolicy, HookPayload, HookPoint, HookRunner, HookSpec
from modex_agent.hook.abc import FinallyTurnHook
from modex_agent.hook.builtin.training_data import TRAINING_RELEVANT_ATTR, TrainingDataHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.ioc.configs.observability import ObservabilityConfig
from modex_agent.runtime.enums import (
    AgentKind,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

_TRACE_ID = "trace-abc-123"
_SESSION_ID = "s1.bot"
_AGENT_NAME = "bot"


class _RecordingOtelStore(OtelSpanTraceStore):
    """In-memory OtelSpanTraceStore: records every save_span and answers queries.

    ``seed()`` pre-populates spans (used to simulate prior chat spans
    being visible to the hook at finally_turn time).
    """

    def __init__(self) -> None:
        # Intentionally do NOT call super().__init__ — we override all I/O.
        self.saved: list[SpanModel] = []
        self._seed: list[SpanModel] = []

    def seed(self, spans: list[SpanModel]) -> None:
        self._seed.extend(spans)

    async def save_span(self, span: SpanModel) -> None:
        self.saved.append(span)

    async def list_by_session(self, session_id: str) -> list[SpanModel]:
        return [s for s in (*self._seed, *self.saved) if s.attributes.get(GenAiAttr.CONVERSATION_ID) == session_id]

    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        return [s for s in (*self._seed, *self.saved) if s.trace_id == trace_id]


class _RaisingOtelStore(_RecordingOtelStore):
    async def save_span(self, span: SpanModel) -> None:
        raise OSError("disk full")


class _RaisingListOtelStore(_RecordingOtelStore):
    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        raise OSError("read error")


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


def _runner(hook: TrainingDataHook) -> HookRunner:
    return HookRunner([HookSpec(hook=hook, on_error=HookErrorPolicy.LOG)])


async def _fire(
    ctx: AgentContext,
    hook: TrainingDataHook,
    result: AgentResult | None,
) -> None:
    payload = HookPayload(data={"result": result})
    await _runner(hook).dispatch(HookPoint.FINALLY_TURN, ctx, payload)


def _chat_span(
    *,
    trace_id: str = _TRACE_ID,
    input_tokens: int = 0,
    output_tokens: int = 0,
    span_id: str = "chat-span",
) -> SpanModel:
    attrs: dict[str, object] = {
        GenAiAttr.AGENT_NAME: _AGENT_NAME,
        GenAiAttr.CONVERSATION_ID: _SESSION_ID,
        GenAiAttr.USAGE_INPUT_TOKENS: input_tokens,
        GenAiAttr.USAGE_OUTPUT_TOKENS: output_tokens,
    }
    return SpanModel(
        trace_id=trace_id,
        span_id=span_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=1000.0,
        end_time=1001.0,
        attributes=attrs,
        status=SpanStatus(code=SpanStatusCode.OK),
    )


def _desc() -> AgentDescriptor:
    return AgentDescriptor(
        address=AgentAddress(name="main"),
        execution_strategy=ExecutionStrategyKind.REACT,
        comm_kind=AgentCommKind.NORMAL,
        system_prompt_template="",
    )


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


async def test_none_result_marks_relevant_true() -> None:
    store = _RecordingOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(), result=None)

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


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
    store.seed([
        _chat_span(input_tokens=60_000, output_tokens=0, span_id="sp1"),
        _chat_span(input_tokens=0, output_tokens=50_000, span_id="sp2"),
    ])
    state = _react_state(iteration=2)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is False


async def test_tokens_equal_to_max_tokens_marks_relevant_true() -> None:
    store = _RecordingOtelStore()
    store.seed([_chat_span(input_tokens=60_000, output_tokens=40_000)])
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100_000), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


async def test_token_sum_ignores_non_chat_spans() -> None:
    store = _RecordingOtelStore()
    non_chat = SpanModel(
        trace_id=_TRACE_ID,
        span_id="tool-span",
        name=SpanName.EXECUTE_TOOL.value,
        kind=SpanKind.INTERNAL.value,
        start_time=1000.0,
        attributes={
            GenAiAttr.AGENT_NAME: _AGENT_NAME,
            GenAiAttr.CONVERSATION_ID: _SESSION_ID,
            GenAiAttr.USAGE_INPUT_TOKENS: 999_999,
            GenAiAttr.USAGE_OUTPUT_TOKENS: 999_999,
        },
    )
    store.seed([non_chat, _chat_span(input_tokens=10, output_tokens=20)])
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


async def test_token_sum_ignores_chat_spans_with_missing_usage() -> None:
    store = _RecordingOtelStore()
    no_usage = SpanModel(
        trace_id=_TRACE_ID,
        span_id="chat-no-usage",
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=1000.0,
        attributes={
            GenAiAttr.AGENT_NAME: _AGENT_NAME,
            GenAiAttr.CONVERSATION_ID: _SESSION_ID,
        },
    )
    store.seed([_chat_span(input_tokens=10, output_tokens=20), no_usage])
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=25), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is False


async def test_token_sum_only_counts_same_trace_id() -> None:
    store = _RecordingOtelStore()
    store.seed([_chat_span(trace_id="other-trace", input_tokens=999_999, output_tokens=0)])
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=100), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True


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


async def test_list_by_trace_id_failure_falls_back_to_zero_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _RaisingListOtelStore()
    state = _react_state(iteration=1)
    ctx = _ctx(state, store)

    with caplog.at_level("WARNING", logger="modex_agent.hook.builtin.training_data"):
        await _fire(ctx, TrainingDataHook(max_iterations=20, max_tokens=10), _result())

    span = _training_span(store)
    assert span.attributes[TRAINING_RELEVANT_ATTR] is True
    assert any("failed to query spans for token sum" in r.message for r in caplog.records)


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
# Factory wiring
# ---------------------------------------------------------------------------


async def test_factory_registers_training_data_hook_when_enabled() -> None:
    factory = DefaultAgentFactory(
        observability_config=ObservabilityConfig(
            training_relevant=True,
            training_max_iterations=15,
            training_max_tokens=50_000,
        ),
    )
    instance = await factory.create_agent(_desc(), broker=None)
    assert instance.pipeline is not None
    runner = instance.pipeline.hook_runner
    assert runner is not None
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert TrainingDataHook in kinds


async def test_factory_no_training_data_hook_when_disabled() -> None:
    factory = DefaultAgentFactory(observability_config=ObservabilityConfig(training_relevant=False))
    instance = await factory.create_agent(_desc(), broker=None)
    assert instance.pipeline is not None
    runner = instance.pipeline.hook_runner
    assert runner is not None
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert TrainingDataHook not in kinds


async def test_factory_disabled_by_default() -> None:
    factory = DefaultAgentFactory()
    instance = await factory.create_agent(_desc(), broker=None)
    runner = instance.pipeline.hook_runner
    assert runner is not None
    kinds = {type(s.hook) for s in runner.hook_specs}
    assert TrainingDataHook not in kinds


async def test_factory_passes_max_iterations_and_max_tokens_to_hook() -> None:
    factory = DefaultAgentFactory(
        observability_config=ObservabilityConfig(
            training_relevant=True,
            training_max_iterations=7,
            training_max_tokens=42_000,
        ),
    )
    instance = await factory.create_agent(_desc(), broker=None)
    runner = instance.pipeline.hook_runner
    assert runner is not None
    training_hooks = [
        s.hook for s in runner.hook_specs if isinstance(s.hook, TrainingDataHook)
    ]
    assert len(training_hooks) == 1
    hook = training_hooks[0]
    assert hook._max_iterations == 7
    assert hook._max_tokens == 42_000


# ---------------------------------------------------------------------------
# isinstance dispatch contract
# ---------------------------------------------------------------------------


def test_training_data_hook_is_finally_turn_hook() -> None:
    hook = TrainingDataHook()
    assert isinstance(hook, FinallyTurnHook)
    assert hook.name == "training_data"


def test_default_thresholds_match_observability_config() -> None:
    hook = TrainingDataHook()
    assert hook._max_iterations == 20
    assert hook._max_tokens == 100_000


# keep the Any import meaningful for static analyzers
_ANNOT: Any = None
