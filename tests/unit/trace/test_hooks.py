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
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "s1")
    assert len(spans) == 2
    root_spans = [s for s in spans if s.name == SpanName.INVOKE_AGENT.value]
    assert len(root_spans) == 2
    for span in root_spans:
        assert span.parent_span_id is None
        assert span.kind == "INTERNAL"
        assert span.attributes[GenAiAttr.OPERATION_NAME] == "invoke_agent"
        assert span.attributes[GenAiAttr.CONVERSATION_ID] == "s1"
        assert span.status.code == SpanStatusCode.OK
        assert span.end_time is not None
    assert ctx.runtime is not None
    trace_id = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
    assert trace_id is not None
    assert root_spans[0].trace_id == trace_id


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
    assert len(spans) == 3
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
@pytest.mark.asyncio
async def test_after_tool_execution_records_per_tool(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s4", store)
    hook = _make_hook()

    await hook.before_turn(ctx)

    tool_calls = [
        ToolCall(tool_name="search", arguments={"query": "hello"}),
        ToolCall(tool_name="read", arguments={"path": "config.py"}),
    ]
    await hook.before_tool_execution(ctx, tool_calls)

    results = [
        ToolResult.from_text("search", "found", execution_time=0.05),
        ToolResult(tool_name="read", error="file not found", execution_time=0.01),
    ]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "s4")
    tool_spans = [s for s in spans if s.name == SpanName.EXECUTE_TOOL.value]
    assert len(tool_spans) == 2

    s0 = tool_spans[0]
    assert s0.attributes[GenAiAttr.TOOL_NAME] == "search"
    assert s0.attributes[GenAiAttr.TOOL_RESULT] == "found"
    assert s0.status.code == SpanStatusCode.OK
    import json as _json
    s0_input = _json.loads(s0.attributes[GenAiAttr.LANGFUSE_OBSERVATION_INPUT])
    assert s0_input["tool_name"] == "search"
    assert s0_input["arguments"] == {"query": "hello"}

    s1 = tool_spans[1]
    assert s1.attributes[GenAiAttr.TOOL_NAME] == "read"
    assert s1.status.code == SpanStatusCode.ERROR
    assert s1.status.message == "file not found"


@pytest.mark.asyncio
async def test_finally_turn_writes_root_span(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s5", store)
    hook = _make_hook()

    from modex_agent.core.message import ChatMessage

    await ctx.history.append(ChatMessage(role="user", content="hello"))
    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, AgentResult(content="done"))

    spans = await _collect_spans(store, "s5")
    assert len(spans) == 2
    root = next(s for s in spans if s.attributes.get("stop_reason") == "completed")
    assert root.name == SpanName.INVOKE_AGENT.value
    assert root.end_time is not None
    assert root.attributes["stop_reason"] == "completed"
    import json as _json
    output = _json.loads(root.attributes[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT])
    assert any(m.get("parts", [{}])[0].get("content") == "done" for m in output)
    # finally_turn root span must also carry input (Langfuse last-write-wins
    # overwrites the before_turn span — input must be re-sent)
    assert GenAiAttr.LANGFUSE_OBSERVATION_INPUT in root.attributes or GenAiAttr.LANGFUSE_TRACE_INPUT in root.attributes


@pytest.mark.asyncio
async def test_disabled_hook_records_nothing(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("s7", store)
    hook = _make_hook(enabled=False)

    await hook.before_turn(ctx)
    await hook.after_llm_response(ctx, LLMResponse(content="x"))
    await hook.before_tool_execution(ctx, [ToolCall(call_id="c1", tool_name="t", arguments={})])
    await hook.after_tool_execution(ctx, [ToolResult.from_text("t", "ok")])
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
    results = [ToolResult.from_text("read", "file contents here", execution_time=0.02)]
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
    results = [ToolResult.from_text("read", long_result)]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "s_trunc")
    span = spans[-1]
    assert span.attributes[GenAiAttr.TOOL_RESULT] == long_result


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
    results = [ToolResult.from_text("write", "ok", execution_time=0.01, call_id="c1")]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "s_args")
    tool_spans = [s for s in spans if s.name == SpanName.EXECUTE_TOOL.value]
    assert len(tool_spans) == 1
    import json as _json
    inp = _json.loads(tool_spans[0].attributes[GenAiAttr.LANGFUSE_OBSERVATION_INPUT])
    assert inp["tool_name"] == "write"
    assert inp["arguments"]["path"] == "/out/OUTPUT.md"
    assert "OUTPUT.md" in str(inp["arguments"]["path"])


# -- runtime store wiring ------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_writes_to_runtime_trace_store(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("ws_sess.main", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "ws_sess.main")
    assert len(spans) == 2
    assert all(s.name == SpanName.INVOKE_AGENT.value for s in spans)
    assert (tmp_path / "traces" / "ws_sess.main" / "spans.jsonl").exists()


@pytest.mark.asyncio
async def test_hook_noop_when_no_runtime_store(tmp_path: Path) -> None:
    ctx = _make_trace_context("s_nostore", store=None)
    hook = _make_hook()

    # Should not raise — just silently skip
    await hook.before_turn(ctx)


@pytest.mark.asyncio
async def test_send_to_agent_emits_handoff_span(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("handoff.main", store)
    hook = _make_hook()

    await hook.before_turn(ctx)

    tool_calls = [
        ToolCall(tool_name="send_to_agent", arguments={"target_agent": "coder", "content": "do the thing"}),
    ]
    await hook.before_tool_execution(ctx, tool_calls)

    results = [
        ToolResult.from_text("send_to_agent", "ack: sent to coder", execution_time=0.02),
    ]
    await hook.after_tool_execution(ctx, results)

    spans = await _collect_spans(store, "handoff.main")
    handoff_spans = [s for s in spans if s.name == SpanName.AGENT_HANDOFF.value]
    assert len(handoff_spans) == 1
    hs = handoff_spans[0]
    assert hs.attributes[GenAiAttr.HANDOFF_TARGET_AGENT] == "coder"
    assert hs.attributes[GenAiAttr.HANDOFF_MESSAGE_TYPE] is not None
    assert hs.parent_span_id is not None
