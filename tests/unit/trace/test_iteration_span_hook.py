from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.iteration_span_hook import IterationSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationType,
    SpanKind,
    SpanName,
)
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.tools.manager import InMemoryToolManager


def _make_context(*, with_trace: bool = True) -> AgentContext:
    session = SessionInfo(session_id="session.worker", agent_name="worker")
    identity = TurnIdentity(agent_id="worker", session=session, turn_id="turn-1")
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    if with_trace:
        state.custom[TurnCustomKey.TRACE_ID] = "trace-1"
        state.custom[TurnCustomKey.ROOT_SPAN_ID] = "root-1"
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
        identity=identity,
    )


def _make_hook(
    tmp_path: Path,
) -> tuple[IterationSpanHook, TraceSessionState, OtelSpanTraceStore]:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    return IterationSpanHook(session=session, store=store), session, store


async def test_iteration_start_count_equals_end_count(tmp_path: Path) -> None:
    hook, session, store = _make_hook(tmp_path)
    context = _make_context()
    assert context.runtime is not None
    state = context.runtime.state
    assert isinstance(state, ReActTurnState)

    for iteration in range(3):
        state.iteration = iteration
        await hook.before_iteration(context)
        await hook.after_iteration(context)

    spans = await store.list_by_session("session.worker")
    starts = [s for s in spans if s.name == SpanName.ITERATION_START.value]
    ends = [s for s in spans if s.name == SpanName.ITERATION_END.value]
    assert len(starts) == 3
    assert len(ends) == 3
    # No decrement hack: iteration numbers are 0, 1, 2 for both start and end.
    assert [s.attributes[GenAiAttr.ITERATION_NUMBER] for s in starts] == [0, 1, 2]
    assert [s.attributes[GenAiAttr.ITERATION_NUMBER] for s in ends] == [0, 1, 2]
    # start_time cache is popped after each iteration.
    assert "trace-1" not in session.iteration_start_times


async def test_iteration_spans_have_correct_parent(tmp_path: Path) -> None:
    hook, _, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.before_iteration(context)
    await hook.after_iteration(context)

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 2
    for span in spans:
        assert span.parent_span_id == "root-1"
        assert span.kind == SpanKind.INTERNAL.value
        assert (
            span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE]
            == LangfuseObservationType.SPAN.value
        )
    end = next(s for s in spans if s.name == SpanName.ITERATION_END.value)
    assert GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT in end.attributes


async def test_iteration_spans_not_emitted_without_trace_id(tmp_path: Path) -> None:
    hook, session, store = _make_hook(tmp_path)
    context = _make_context(with_trace=False)

    await hook.before_iteration(context)
    await hook.after_iteration(context)

    assert await store.list_by_session("session.worker") == []
    assert session.iteration_start_times == {}
