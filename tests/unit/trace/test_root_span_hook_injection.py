from __future__ import annotations

from unittest.mock import AsyncMock, call

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.score_injector import L2ScoreInjector
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.store import SpanModel, SpanStatus

_TRACE_ID = "shared-trace"


def _make_context(
    trace_id: str,
    root_span_id: str,
    parent_span_id: str | None = None,
) -> AgentContext:
    session = SessionInfo(
        session_id=f"session.{root_span_id}",
        agent_name=root_span_id,
        parent_session_id="session.parent" if parent_span_id is not None else None,
    )
    state = ReActTurnState(
        identity=TurnIdentity(agent_id=root_span_id, session=session, turn_id="turn-1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    state.custom[TurnCustomKey.TRACE_ID] = trace_id
    state.custom[TurnCustomKey.ROOT_SPAN_ID] = root_span_id
    if parent_span_id is not None:
        state.custom[TurnCustomKey.PARENT_SPAN_ID] = parent_span_id
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


def _span(span_id: str, parent_span_id: str | None, name: SpanName) -> SpanModel:
    return SpanModel(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name.value,
        start_time=1.0,
        end_time=2.0,
    )


async def test_single_root_injects_score_for_only_its_subtree() -> None:
    root_span = _span("root", None, SpanName.INVOKE_AGENT)
    chat_span = _span("chat", "root", SpanName.CHAT).model_copy(
        update={
            "attributes": {
                GenAiAttr.USAGE_REASONING_TOKENS.value: 12,
                GenAiAttr.USAGE_INPUT_TOKENS.value: 20,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 10,
                GenAiAttr.OUTPUT_MESSAGES.value: [
                    {"role": "assistant", "parts": [{"type": "text", "content": "done"}]}
                ],
            }
        }
    )
    tool_span = _span("tool", "chat", SpanName.EXECUTE_TOOL)
    subtree = [root_span, chat_span, tool_span]
    store = AsyncMock(spec=OtelSpanTraceStore)
    store.list_by_trace_id.return_value = subtree
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_span.span_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_span.span_id),
        result=AgentResult(content="done"),
    )

    injector.inject_scores.assert_awaited_once_with(
        _TRACE_ID,
        subtree,
        observation_id=root_span.span_id,
    )


async def test_shared_trace_injects_each_root_without_cross_contamination() -> None:
    parent_root = _span("parent-root", None, SpanName.INVOKE_AGENT)
    parent_chat = _span("parent-chat", parent_root.span_id, SpanName.CHAT).model_copy(
        update={
            "attributes": {
                GenAiAttr.USAGE_REASONING_TOKENS.value: 10,
                GenAiAttr.USAGE_INPUT_TOKENS.value: 80,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 20,
                GenAiAttr.OUTPUT_MESSAGES.value: [
                    {"role": "assistant", "parts": [{"type": "text", "content": "parent"}]}
                ],
            }
        }
    )
    parent_tool = _span("parent-tool", parent_chat.span_id, SpanName.EXECUTE_TOOL)
    handoff = _span("handoff", parent_root.span_id, SpanName.AGENT_HANDOFF)
    child_root = _span("child-root", handoff.span_id, SpanName.INVOKE_AGENT)
    child_chat = _span("child-chat", child_root.span_id, SpanName.CHAT).model_copy(
        update={
            "attributes": {
                GenAiAttr.USAGE_REASONING_TOKENS.value: 200,
                GenAiAttr.USAGE_INPUT_TOKENS.value: 40,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 10,
                GenAiAttr.OUTPUT_MESSAGES.value: [
                    {"role": "assistant", "parts": [{"type": "text", "content": "child"}]}
                ],
            }
        }
    )
    child_tool = _span("child-tool", child_chat.span_id, SpanName.EXECUTE_TOOL).model_copy(
        update={"status": SpanStatus(code=SpanStatusCode.ERROR)}
    )
    parent_subtree = [parent_root, parent_chat, parent_tool, handoff]
    child_subtree = [child_root, child_chat, child_tool]
    store = AsyncMock(spec=OtelSpanTraceStore)
    store.list_by_trace_id.return_value = [*parent_subtree, *child_subtree]
    injector = AsyncMock(spec=L2ScoreInjector)
    parent_session = TraceSessionState()
    parent_session.root_span_info[_TRACE_ID] = (parent_root.span_id, 1.0)
    child_session = TraceSessionState()
    child_session.root_span_info[_TRACE_ID] = (child_root.span_id, 1.0)
    parent_hook = RootSpanHook(session=parent_session, store=store, score_injector=injector)
    child_hook = RootSpanHook(session=child_session, store=store, score_injector=injector)

    await parent_hook.finally_graph(
        _make_context(_TRACE_ID, parent_root.span_id),
        result=AgentResult(content="parent"),
    )
    await child_hook.finally_graph(
        _make_context(_TRACE_ID, child_root.span_id, handoff.span_id),
        result=AgentResult(content="child"),
    )

    assert injector.inject_scores.await_count == 2
    injector.inject_scores.assert_has_awaits(
        [
            call(
                _TRACE_ID,
                parent_subtree,
                observation_id=parent_root.span_id,
            ),
            call(
                _TRACE_ID,
                child_subtree,
                observation_id=child_root.span_id,
            ),
        ]
    )


async def test_injector_failure_does_not_escape_finally_graph() -> None:
    root_span = _span("root", None, SpanName.INVOKE_AGENT)
    store = AsyncMock(spec=OtelSpanTraceStore)
    store.list_by_trace_id.return_value = [root_span]
    injector = AsyncMock(spec=L2ScoreInjector)
    injector.inject_scores.side_effect = RuntimeError("injection failed")
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_span.span_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_span.span_id),
        result=AgentResult(content="done"),
    )


async def test_no_injector_performs_no_score_query_work() -> None:
    root_span = _span("root", None, SpanName.INVOKE_AGENT)
    store = AsyncMock(spec=OtelSpanTraceStore)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_span.span_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=None)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_span.span_id),
        result=AgentResult(content="done"),
    )

    store.list_by_trace_id.assert_not_awaited()


async def test_non_completed_root_skips_score_injection() -> None:
    root_span = _span("root", None, SpanName.INVOKE_AGENT)
    store = AsyncMock(spec=OtelSpanTraceStore)
    store.list_by_trace_id.return_value = [root_span]
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_span.span_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_span.span_id),
        result=AgentResult(content="failed", stop_reason=StopReason.ERROR),
    )

    store.list_by_trace_id.assert_not_awaited()
    injector.inject_scores.assert_not_awaited()
