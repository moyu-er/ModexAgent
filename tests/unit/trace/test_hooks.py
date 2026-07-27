"""Tests for TraceCollectorHook."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig, ToolResult
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.hooks import TraceCollectorHook
from modex_agent.trace.otel_store import OtelSpanTraceStore, SpanModel
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode

# -- helpers ------------------------------------------------------------------


def _make_trace_context(session_id: str, store: OtelSpanTraceStore | None = None) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str(session_id), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    if store is not None:
        services.trace_store = store
    runtime = AgentRuntime(services=services, state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(config=ToolManagerConfig()),
        session=SessionInfo.from_str(session_id),
        runtime=runtime,
    )


def _make_hook(*, enabled: bool = True) -> TraceCollectorHook:
    return TraceCollectorHook(enabled=enabled)


def _make_store(tmp_path: Path) -> OtelSpanTraceStore:
    return OtelSpanTraceStore(base_dir=tmp_path / "traces")


async def _collect_spans(store: OtelSpanTraceStore, session_id: str) -> list[SpanModel]:
    return await store.list_by_session(session_id)


# -- tests --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_turn_records_turn_start(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s1", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    # Root span is written at finally_turn, not before_turn
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "s1")
    assert len(spans) == 1
    span = spans[0]
    assert span.name == SpanName.INVOKE_AGENT.value
    assert span.parent_span_id is None
    assert span.kind == "INTERNAL"
    assert span.attributes[GenAiAttr.OPERATION_NAME] == "invoke_agent"
    assert span.attributes[GenAiAttr.CONVERSATION_ID] == "s1"
    assert span.status.code == SpanStatusCode.OK
    assert span.end_time is not None
    # trace_id should be stored in turn state
    assert ctx.runtime is not None
    trace_id = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
    assert trace_id is not None
    assert span.trace_id == trace_id


@pytest.mark.asyncio
async def test_after_llm_response_records_llm_call(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s2", store)
    hook = _make_hook()

    await hook.before_turn(ctx)

    response = LLMResponse(
        content="hello",
        tool_calls=[ToolCall(call_id="c1", tool_name="search", arguments={"q": "test"})],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    await hook.after_llm_response(ctx, response)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "s2")
    # LLM call + root span (written at finally_turn)
    assert len(spans) == 2
    llm_span = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert llm_span.kind == "CLIENT"
    assert llm_span.attributes[GenAiAttr.OUTPUT_MESSAGES][0]["parts"][0]["content"] == "hello"
    assert llm_span.attributes[GenAiAttr.OUTPUT_TOOL_CALLS] == [
        {"tool_name": "search", "arguments": '{"q": "test"}'},
    ]
    assert llm_span.attributes[GenAiAttr.USAGE_INPUT_TOKENS] == 10
    assert llm_span.attributes[GenAiAttr.USAGE_OUTPUT_TOKENS] == 5
    root_span = next(s for s in spans if s.name == SpanName.INVOKE_AGENT.value)
    assert llm_span.parent_span_id == root_span.span_id


@pytest.mark.asyncio
async def test_before_tool_execution_records_tool_batch(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s3", store)
    hook = _make_hook()

    await hook.before_turn(ctx)

    tool_calls = [
        ToolCall(call_id="c1", tool_name="search", arguments={"q": "a"}),
        ToolCall(call_id="c2", tool_name="read", arguments={"path": "/tmp"}),
    ]
    await hook.before_tool_execution(ctx, tool_calls)
    await hook.after_tool_execution(ctx, [])

    spans = await _collect_spans(store, "s3")
    span = spans[-1]
    assert span.name == SpanName.EXECUTE_TOOL_BATCH.value
    assert span.attributes["tool_count"] == 2
    assert span.attributes["tool_names"] == ["search", "read"]


@pytest.mark.asyncio
async def test_after_tool_execution_records_per_tool(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s4", store)
    hook = _make_hook()

    await hook.before_turn(ctx)

    results = [
        ToolResult(tool_name="search", result="found", execution_time=0.05),
        ToolResult(tool_name="read", error="file not found", execution_time=0.01),
    ]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "s4")
    # 2 tool results (root span not yet written — only at finally_turn)
    assert len(spans) == 2
    s0 = spans[0]
    assert s0.name == SpanName.EXECUTE_TOOL.value
    assert s0.attributes[GenAiAttr.TOOL_NAME] == "search"
    assert s0.attributes[GenAiAttr.TOOL_RESULT] == "found"
    assert s0.status.code == SpanStatusCode.OK

    s1 = spans[1]
    assert s1.name == SpanName.EXECUTE_TOOL.value
    assert s1.attributes[GenAiAttr.TOOL_NAME] == "read"
    assert s1.status.code == SpanStatusCode.ERROR
    assert s1.status.message == "file not found"


@pytest.mark.asyncio
async def test_finally_turn_writes_root_span(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s5", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, AgentResult(content="done"))

    spans = await _collect_spans(store, "s5")
    # Root invoke_agent span written at finally_turn with stop_reason + end_time
    assert len(spans) == 1
    root = spans[0]
    assert root.name == SpanName.INVOKE_AGENT.value
    assert root.end_time is not None
    assert root.attributes["stop_reason"] == "completed"
    assert root.attributes[GenAiAttr.OUTPUT_MESSAGES][0]["parts"][0]["content"] == "done"


@pytest.mark.asyncio
async def test_disabled_hook_records_nothing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s7", store)
    hook = _make_hook(enabled=False)

    await hook.before_turn(ctx)
    await hook.after_llm_response(ctx, LLMResponse(content="x"))
    await hook.before_tool_execution(ctx, [ToolCall(call_id="c1", tool_name="t", arguments={})])
    await hook.after_tool_execution(ctx, [ToolResult(tool_name="t", result="ok")])
    await hook.finally_turn(ctx, AgentResult(content="done"))

    spans = await _collect_spans(store, "s7")
    assert len(spans) == 0


# -- trace content: tool calls, results, messages ------------------------------


@pytest.mark.asyncio
async def test_llm_response_captures_tool_call_arguments(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s_tc", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    response = LLMResponse(
        content="Let me search and read.",
        tool_calls=[
            ToolCall(call_id="c1", tool_name="search", arguments={"q": "bug"}),
            ToolCall(call_id="c2", tool_name="read", arguments={"path": "/tmp/x"}),
        ],
        finish_reason="tool_calls",
    )
    await hook.after_llm_response(ctx, response)

    spans = await _collect_spans(store, "s_tc")
    span = spans[-1]
    assert span.name == SpanName.CHAT.value
    assert span.attributes[GenAiAttr.OUTPUT_MESSAGES][0]["parts"][0]["content"] == "Let me search and read."
    tool_calls = span.attributes[GenAiAttr.OUTPUT_TOOL_CALLS]
    assert len(tool_calls) == 2
    assert tool_calls[0]["tool_name"] == "search"
    assert "bug" in tool_calls[0]["arguments"]
    assert tool_calls[1]["tool_name"] == "read"
    assert "/tmp/x" in tool_calls[1]["arguments"]


@pytest.mark.asyncio
async def test_llm_response_captures_reasoning_content(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s_reason", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    response = LLMResponse(content="Answer.", reasoning_content="Step 1: think\nStep 2: conclude")
    await hook.after_llm_response(ctx, response)

    spans = await _collect_spans(store, "s_reason")
    span = spans[-1]
    assert "Step 1: think" in span.attributes[GenAiAttr.OUTPUT_REASONING_CONTENT]


@pytest.mark.asyncio
async def test_tool_execution_captures_result_content(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s_result", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    results = [ToolResult(tool_name="read", result="file contents here", execution_time=0.02)]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "s_result")
    span = spans[-1]
    assert span.attributes[GenAiAttr.TOOL_NAME] == "read"
    assert span.attributes[GenAiAttr.TOOL_RESULT] == "file contents here"
    assert span.end_time is not None


@pytest.mark.asyncio
async def test_tool_execution_result_truncated(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s_trunc", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    long_result = "x" * 5000
    results = [ToolResult(tool_name="read", result=long_result)]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "s_trunc")
    span = spans[-1]
    assert "truncated" in span.attributes[GenAiAttr.TOOL_RESULT]
    assert len(span.attributes[GenAiAttr.TOOL_RESULT]) < len(long_result)


@pytest.mark.asyncio
async def test_before_tool_execution_captures_full_arguments(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s_args", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    tool_calls = [
        ToolCall(
            call_id="c1", tool_name="write", arguments={"path": "/out/OUTPUT.md", "content": "done"}
        ),
    ]
    await hook.before_tool_execution(ctx, tool_calls)
    await hook.after_tool_execution(ctx, [])

    spans = await _collect_spans(store, "s_args")
    span = spans[-1]
    assert span.attributes["tool_names"] == ["write"]
    tool_args = span.attributes["tool_arguments"]
    assert len(tool_args) == 1
    assert tool_args[0]["tool_name"] == "write"
    assert "OUTPUT.md" in tool_args[0]["arguments"]


# -- runtime store wiring ------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_writes_to_runtime_trace_store(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("ws_sess.main", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "ws_sess.main")
    assert len(spans) == 1
    assert spans[0].name == SpanName.INVOKE_AGENT.value
    assert (tmp_path / "traces" / "ws_sess.main" / "spans.jsonl").exists()


@pytest.mark.asyncio
async def test_hook_noop_when_no_runtime_store(tmp_path: Path) -> None:
    ctx = _make_trace_context("s_nostore", store=None)
    hook = _make_hook()

    # Should not raise — just silently skip
    await hook.before_turn(ctx)
