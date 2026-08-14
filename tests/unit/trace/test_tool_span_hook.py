from __future__ import annotations

import json
from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig, ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr, LangfuseObservationType, SpanName
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.tool_span_hook import ToolSpanHook


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
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
        identity=identity,
    )


def _make_hook(
    tmp_path: Path,
) -> tuple[ToolSpanHook, TraceSessionState, OtelSpanTraceStore]:
    session = TraceSessionState()
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    return ToolSpanHook(session=session, store=store), session, store


def _tool_calls() -> list[ToolCall]:
    return [
        ToolCall(tool_name="search", arguments={"query": "trace"}, call_id="call-1"),
        ToolCall(tool_name="write", arguments={"path": "out.txt"}, call_id="call-2"),
    ]


def _tool_results() -> list[ToolResult]:
    return [
        ToolResult.from_text(
            "search",
            "found",
            call_id="call-1",
            execution_time=0.02,
        ),
        ToolResult(
            tool_name="write",
            call_id="call-2",
            error="permission denied",
            execution_time=0.03,
        ),
    ]


async def test_tool_batch_span_emitted(tmp_path: Path) -> None:
    hook, session, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.before_tool_execution(context, _tool_calls())
    await hook.after_tool_execution(context, _tool_results())

    spans = await store.list_by_session("session.worker")
    batch = next(span for span in spans if span.name == SpanName.EXECUTE_TOOL_BATCH.value)
    assert batch.parent_span_id == "root-1"
    assert batch.attributes["gen_ai.tool.count"] == 2
    assert batch.attributes["gen_ai.tool.names"] == ["search", "write"]
    assert (
        batch.attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] == LangfuseObservationType.SPAN.value
    )


async def test_per_tool_spans_parent_is_batch(tmp_path: Path) -> None:
    hook, _, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.before_tool_execution(context, _tool_calls())
    await hook.after_tool_execution(context, _tool_results())

    spans = await store.list_by_session("session.worker")
    batch = next(span for span in spans if span.name == SpanName.EXECUTE_TOOL_BATCH.value)
    tools = [span for span in spans if span.name == SpanName.EXECUTE_TOOL.value]
    assert len(tools) == 2
    assert all(span.parent_span_id == batch.span_id for span in tools)
    assert json.loads(str(tools[0].attributes[GenAiAttr.TOOL_CALL_ARGUMENTS])) == {"query": "trace"}
    assert tools[0].attributes[GenAiAttr.TOOL_RESULT] == "found"
    assert tools[0].attributes[GenAiAttr.TOOL_SUCCESS] is True
    assert tools[1].attributes[GenAiAttr.TOOL_FAIL] is True
    assert tools[1].attributes[GenAiAttr.TOOL_ERROR_TYPE] == "permission denied"
    assert tools[1].attributes["gen_ai.tool.execution_time"] == 0.03


async def test_no_tools_no_batch_span(tmp_path: Path) -> None:
    hook, session, store = _make_hook(tmp_path)
    context = _make_context()

    await hook.before_tool_execution(context, [])
    await hook.after_tool_execution(context, [])

    assert await store.list_by_session("session.worker") == []
    assert "trace-1" not in session.tool_batch_info


async def test_tool_spans_not_emitted_without_trace_id(tmp_path: Path) -> None:
    hook, session, store = _make_hook(tmp_path)
    context = _make_context(with_trace=False)

    await hook.before_tool_execution(context, _tool_calls())
    await hook.after_tool_execution(context, _tool_results())

    assert await store.list_by_session("session.worker") == []
    assert session.tool_batch_info == {}
