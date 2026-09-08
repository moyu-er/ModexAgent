from __future__ import annotations

from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ToolCall
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ToolResult
from modex_agent.hook.abc import HookPayload, HookPoint, HookSpec
from modex_agent.hook.runner import HookRunner
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.handoff_span_hook import HandoffSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.tool_span_hook import ToolSpanHook


def _make_context(*, with_trace: bool = True) -> AgentContext:
    session = SessionInfo(session_id="session.main", agent_name="main")
    identity = TurnIdentity(agent_id="main", session=session, turn_id="parent-turn")
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


def _dispatch_call() -> ToolCall:
    return ToolCall(
        tool_name="task",
        arguments={"target_agent": "worker", "content": "inspect tracing"},
        call_id="call-1",
    )


def _dispatch_result() -> ToolResult:
    return ToolResult.from_text(
        "task",
        "Task dispatched to 'worker'.\n\ninvocation_id: child-turn-7",
        call_id="call-1",
        execution_time=0.01,
    )


async def _emit_dispatch_spans(
    tmp_path: Path,
) -> tuple[AgentContext, TraceSessionState, OtelSpanTraceStore]:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    tool_hook = ToolSpanHook(session=session, store=store)
    handoff_hook = HandoffSpanHook(session=session, store=store)
    context = _make_context()
    calls = [_dispatch_call()]
    results = [_dispatch_result()]

    await tool_hook.before_tool_execution(context, calls)
    await tool_hook.after_tool_execution(context, results)
    await handoff_hook.after_tool_execution(context, results)
    return context, session, store


async def test_handoff_span_emitted_for_dispatch(tmp_path: Path) -> None:
    context, _, store = await _emit_dispatch_spans(tmp_path)

    spans = await store.list_by_session("session.main")
    batch = next(span for span in spans if span.name == SpanName.EXECUTE_TOOL_BATCH.value)
    handoff = next(span for span in spans if span.name == SpanName.AGENT_HANDOFF.value)
    assert handoff.parent_span_id == batch.span_id
    assert handoff.trace_id == batch.trace_id == "trace-1"
    assert context.runtime is not None
    assert context.runtime.state.custom[TurnCustomKey.HANDOFF_SPAN_ID] == handoff.span_id


async def test_registered_hook_order_links_handoff_to_batch(tmp_path: Path) -> None:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    tool_hook = ToolSpanHook(session=session, store=store)
    handoff_hook = HandoffSpanHook(session=session, store=store)
    runner = HookRunner([HookSpec(hook=tool_hook), HookSpec(hook=handoff_hook)])
    context = _make_context()
    calls = [_dispatch_call()]
    results = [_dispatch_result()]

    await runner.dispatch(
        HookPoint.BEFORE_TOOL_EXECUTION,
        context,
        HookPayload(data={"tool_calls": calls}),
    )
    await runner.dispatch(
        HookPoint.AFTER_TOOL_EXECUTION,
        context,
        HookPayload(data={"results": results}),
    )

    spans = await store.list_by_session("session.main")
    batch = next(span for span in spans if span.name == SpanName.EXECUTE_TOOL_BATCH.value)
    handoff = next(span for span in spans if span.name == SpanName.AGENT_HANDOFF.value)
    assert handoff.parent_span_id == batch.span_id
    assert handoff.attributes[GenAiAttr.HANDOFF_TARGET_AGENT] == "worker"


async def test_handoff_attributes_not_unknown(tmp_path: Path) -> None:
    _, _, store = await _emit_dispatch_spans(tmp_path)

    spans = await store.list_by_session("session.main")
    handoff = next(span for span in spans if span.name == SpanName.AGENT_HANDOFF.value)
    attributes = handoff.attributes
    expected = {
        GenAiAttr.HANDOFF_TARGET_AGENT: "worker",
        GenAiAttr.HANDOFF_TARGET_KIND: "subagent",
        GenAiAttr.HANDOFF_MESSAGE_TYPE: "task_request",
        GenAiAttr.HANDOFF_PARENT_TURN_ID: "parent-turn",
        GenAiAttr.HANDOFF_CHILD_TURN_ID: "child-turn-7",
        GenAiAttr.HANDOFF_CHILD_TRACE_ID: "trace-1",
    }
    assert {key: attributes[key] for key in expected} == expected
    assert "unknown" not in expected.values()


async def test_no_handoff_when_no_dispatch_tool(tmp_path: Path) -> None:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    tool_hook = ToolSpanHook(session=session, store=store)
    handoff_hook = HandoffSpanHook(session=session, store=store)
    context = _make_context()
    calls = [ToolCall(tool_name="search", arguments={"query": "trace"}, call_id="call-1")]
    results = [ToolResult.from_text("search", "found", call_id="call-1")]

    await tool_hook.before_tool_execution(context, calls)
    await handoff_hook.after_tool_execution(context, results)

    spans = await store.list_by_session("session.main")
    assert all(span.name != SpanName.AGENT_HANDOFF.value for span in spans)
    assert context.runtime is not None
    assert TurnCustomKey.HANDOFF_SPAN_ID not in context.runtime.state.custom


async def test_handoff_uses_root_when_batch_is_unavailable(tmp_path: Path) -> None:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    hook = HandoffSpanHook(session=session, store=store)
    context = _make_context()

    await hook.after_tool_execution(context, [_dispatch_result()])

    spans = await store.list_by_session("session.main")
    assert len(spans) == 1
    assert spans[0].name == SpanName.AGENT_HANDOFF.value
    assert spans[0].parent_span_id == "root-1"
    assert spans[0].attributes[GenAiAttr.HANDOFF_TARGET_AGENT] == "worker"


async def test_handoff_not_emitted_without_trace_id(tmp_path: Path) -> None:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    hook = HandoffSpanHook(session=session, store=store)
    context = _make_context(with_trace=False)

    await hook.after_tool_execution(context, [_dispatch_result()])

    assert await store.list_by_session("session.main") == []


async def test_handoff_hook_no_store_fallback(tmp_path: Path) -> None:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    tool_hook = ToolSpanHook(session=session, store=store)
    handoff_hook = HandoffSpanHook(session=session, store=store)
    context = _make_context()
    calls = [_dispatch_call()]
    results = [_dispatch_result()]

    await tool_hook.before_tool_execution(context, calls)
    await tool_hook.after_tool_execution(context, results)

    # batch spans are in the store; clearing session forces root fallback, not store query
    session.tool_batch_info.clear()

    await handoff_hook.after_tool_execution(context, results)

    spans = await store.list_by_session("session.main")
    handoffs = [s for s in spans if s.name == SpanName.AGENT_HANDOFF.value]
    assert len(handoffs) == 1
    assert handoffs[0].parent_span_id == "root-1"
