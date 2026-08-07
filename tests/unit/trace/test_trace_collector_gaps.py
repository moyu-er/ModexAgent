"""Tests for TraceCollectorHook G1/G2/G3/G5 gap remediation + new attributes.

Covers:
- G1: LLM call wall-clock duration (api_duration_s + end_time on chat span)
- G2: Request prompt capture via PromptCaptureStrategy
- G3: human.review approval span with decision/deny_reason/tool_name/tool_call_id
- G5: iteration.start/iteration.end boundary spans with iteration_number
- New attributes: cache tokens, gen_ai.request.model, tool success/fail/error_type,
  execute_tool_batch end_time
- PromptCaptureStrategy ABC subclass replaces capture logic without hook code change
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.approval.constants import (
    ApprovalDecision,
    ApprovalStatus,
    ApprovalTier,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig, ToolResult
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import (
    ApprovalRequestState,
    ApprovalTransaction,
    ToolArguments,
    TurnIdentity,
)
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.hooks import TraceCollectorHook
from modex_agent.trace.otel_store import OtelSpanTraceStore, SpanModel
from modex_agent.trace.prompt_capture import (
    PromptCaptureStrategy,
    SummaryPromptCapture,
    build_prompt_capture,
)
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode

# -- helpers ------------------------------------------------------------------


def _make_trace_context(
    session_id: str,
    store: OtelSpanTraceStore | None = None,
) -> AgentContext:
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


def _make_store(tmp_path: Path) -> OtelSpanTraceStore:
    return OtelSpanTraceStore(base_dir=tmp_path / "traces")


def _make_hook(
    *,
    enabled: bool = True,
    prompt_capture: PromptCaptureStrategy | None = None,
    model: str | None = None,
) -> TraceCollectorHook:
    return TraceCollectorHook(
        enabled=enabled,
        prompt_capture=prompt_capture,
        model=model,
    )


async def _collect_spans(store: OtelSpanTraceStore, session_id: str) -> list[SpanModel]:
    return await store.list_by_session(session_id)


def _set_iteration(ctx: AgentContext, n: int) -> None:
    assert ctx.runtime is not None
    ctx.runtime.state.iteration = n


# -- G1: LLM duration --------------------------------------------------------


@pytest.mark.asyncio
async def test_g1_llm_duration_api_duration_s_and_end_time(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g1", store)
    hook = _make_hook(model="test-model")

    await hook.before_turn(ctx)
    request = [ChatMessage(role=MessageRole.USER, content="hello")]
    await hook.before_llm(ctx, request)
    response = LLMResponse(content="hi", usage={"input_tokens": 5, "output_tokens": 3})
    await hook.after_llm_response(ctx, response)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g1")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert GenAiAttr.API_DURATION_S in chat.attributes
    assert chat.attributes[GenAiAttr.API_DURATION_S] >= 0
    assert chat.end_time is not None
    assert chat.end_time >= chat.start_time


@pytest.mark.asyncio
async def test_g1_llm_duration_not_set_without_before_llm(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g1_nobefore", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    response = LLMResponse(content="hi")
    await hook.after_llm_response(ctx, response)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g1_nobefore")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert GenAiAttr.API_DURATION_S not in chat.attributes


# -- G2: Request prompt capture ----------------------------------------------


@pytest.mark.asyncio
async def test_g2_prompt_capture_summary_records_messages(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g2", store)
    strategy = SummaryPromptCapture(max_messages=3)
    hook = _make_hook(prompt_capture=strategy, model="gpt-4")

    await hook.before_turn(ctx)
    request = [
        ChatMessage(role=MessageRole.SYSTEM, content="You are helpful."),
        ChatMessage(role=MessageRole.USER, content="hello world"),
    ]
    await hook.before_llm(ctx, request)
    response = LLMResponse(content="hi")
    await hook.after_llm_response(ctx, response)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g2")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert GenAiAttr.REQUEST_MODEL in chat.attributes
    assert chat.attributes[GenAiAttr.REQUEST_MODEL] == "gpt-4"
    assert GenAiAttr.INPUT_MESSAGES in chat.attributes
    messages = chat.attributes[GenAiAttr.INPUT_MESSAGES]
    assert isinstance(messages, list)
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["parts"][0]["content"] == "hello world"


@pytest.mark.asyncio
async def test_g2_system_prompt_hashed_not_recorded(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g2_sys", store)
    strategy = SummaryPromptCapture()
    hook = _make_hook(prompt_capture=strategy, model="gpt-4")

    await hook.before_turn(ctx)
    system_content = "You are a secret agent."
    request = [
        ChatMessage(role=MessageRole.SYSTEM, content=system_content),
        ChatMessage(role=MessageRole.USER, content="go"),
    ]
    await hook.before_llm(ctx, request)
    await hook.after_llm_response(ctx, LLMResponse(content="ok"))
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g2_sys")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert GenAiAttr.SYSTEM_PROMPT_HASH in chat.attributes
    assert len(str(chat.attributes[GenAiAttr.SYSTEM_PROMPT_HASH])) == 16
    assert GenAiAttr.SYSTEM_PROMPT_LENGTH in chat.attributes
    assert chat.attributes[GenAiAttr.SYSTEM_PROMPT_LENGTH] == len(system_content)
    messages = chat.attributes[GenAiAttr.INPUT_MESSAGES]
    assert all(m["role"] != "system" for m in messages)


@pytest.mark.asyncio
async def test_g2_prompt_capture_truncates_long_content(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g2_trunc", store)
    strategy = SummaryPromptCapture(max_text_chars=50)
    hook = _make_hook(prompt_capture=strategy, model="m")

    await hook.before_turn(ctx)
    long_text = "x" * 200
    request = [ChatMessage(role=MessageRole.USER, content=long_text)]
    await hook.before_llm(ctx, request)
    await hook.after_llm_response(ctx, LLMResponse(content="r"))
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g2_trunc")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    messages = chat.attributes[GenAiAttr.INPUT_MESSAGES]
    assert "truncated" in messages[0]["parts"][0]["content"]
    assert len(messages[0]["parts"][0]["content"]) < len(long_text)


@pytest.mark.asyncio
async def test_g2_prompt_capture_strategy_abc_replaces_logic(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g2_abc", store)

    class CustomCapture(PromptCaptureStrategy):
        def capture(
            self,
            messages: Sequence[ChatMessage],
            model: str | None,
        ) -> dict[str, object]:
            return {
                GenAiAttr.REQUEST_MODEL: "custom-model",
                "custom.captured_count": len(messages),
            }

    hook = _make_hook(prompt_capture=CustomCapture(), model="original")
    await hook.before_turn(ctx)
    request = [
        ChatMessage(role=MessageRole.USER, content="a"),
        ChatMessage(role=MessageRole.USER, content="b"),
    ]
    await hook.before_llm(ctx, request)
    await hook.after_llm_response(ctx, LLMResponse(content="r"))
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g2_abc")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert chat.attributes[GenAiAttr.REQUEST_MODEL] == "custom-model"
    assert chat.attributes["custom.captured_count"] == 2


# -- G3: Approval span --------------------------------------------------------


@pytest.mark.asyncio
async def test_g3_approval_span_records_decision_and_deny_reason(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g3", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    transaction = ApprovalTransaction(
        approval_id="ap1",
        turn_id="t1",
        subject_type="tool_call",
        subject_ids=["c1"],
        requests=[
            ApprovalRequestState(
                request_id="r1",
                approval_id="ap1",
                tool_call_id="c1",
                tool_name="write_file",
                arguments=ToolArguments(values={}),
                tier=ApprovalTier.DANGEROUS,
                iteration=1,
            )
        ],
        decisions={"c1": ApprovalDecision.DENIED},
        status=ApprovalStatus.DENIED,
        deny_reason="User rejected",
    )
    await hook.after_approval(ctx, transaction)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g3")
    approval_span = next(s for s in spans if s.name == SpanName.HUMAN_REVIEW.value)
    assert approval_span.attributes[GenAiAttr.APPROVAL_DECISION] == "denied"
    assert approval_span.attributes[GenAiAttr.APPROVAL_DENY_REASON] == "User rejected"
    assert approval_span.attributes[GenAiAttr.APPROVAL_TOOL_NAME] == "write_file"
    assert approval_span.attributes[GenAiAttr.APPROVAL_TOOL_CALL_ID] == "c1"


@pytest.mark.asyncio
async def test_g3_approval_span_approved_no_deny_reason(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g3_ok", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    transaction = ApprovalTransaction(
        approval_id="ap2",
        turn_id="t1",
        subject_type="tool_call",
        subject_ids=["c1"],
        requests=[
            ApprovalRequestState(
                request_id="r1",
                approval_id="ap2",
                tool_call_id="c1",
                tool_name="read_file",
                arguments=ToolArguments(values={}),
                tier=ApprovalTier.NORMAL,
                iteration=1,
            )
        ],
        decisions={"c1": ApprovalDecision.ALLOWED},
        status=ApprovalStatus.APPROVED,
    )
    await hook.after_approval(ctx, transaction)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g3_ok")
    approval_span = next(s for s in spans if s.name == SpanName.HUMAN_REVIEW.value)
    assert approval_span.attributes[GenAiAttr.APPROVAL_DECISION] == "approved"
    assert GenAiAttr.APPROVAL_DENY_REASON not in approval_span.attributes


# -- G5: Iteration boundary spans ---------------------------------------------


@pytest.mark.asyncio
async def test_g5_iteration_start_and_end_spans(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g5", store)
    hook = _make_hook()

    await hook.before_turn(ctx)

    _set_iteration(ctx, 1)
    await hook.before_iteration(ctx)

    _set_iteration(ctx, 2)
    await hook.after_iteration(ctx)
    await hook.before_iteration(ctx)

    _set_iteration(ctx, 3)
    await hook.after_iteration(ctx)

    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g5")
    start_spans = [s for s in spans if s.name == SpanName.ITERATION_START.value]
    end_spans = [s for s in spans if s.name == SpanName.ITERATION_END.value]
    assert len(start_spans) == 2
    assert len(end_spans) == 2

    assert start_spans[0].attributes[GenAiAttr.ITERATION_NUMBER] == 1
    assert start_spans[1].attributes[GenAiAttr.ITERATION_NUMBER] == 2

    assert end_spans[0].attributes[GenAiAttr.ITERATION_NUMBER] == 1
    assert end_spans[1].attributes[GenAiAttr.ITERATION_NUMBER] == 2

    for end_span in end_spans:
        assert end_span.end_time is not None
        assert end_span.end_time >= end_span.start_time


@pytest.mark.asyncio
async def test_g5_iteration_spans_parented_to_root(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("g5_parent", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    _set_iteration(ctx, 1)
    await hook.before_iteration(ctx)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "g5_parent")
    root = next(s for s in spans if s.name == SpanName.INVOKE_AGENT.value)
    iter_start = next(s for s in spans if s.name == SpanName.ITERATION_START.value)
    assert iter_start.parent_span_id == root.span_id


# -- New attributes: cache tokens ---------------------------------------------


@pytest.mark.asyncio
async def test_chat_span_records_cache_tokens(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("cache", store)
    hook = _make_hook(model="m")

    await hook.before_turn(ctx)
    response = LLMResponse(
        content="hi",
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 20,
        },
    )
    await hook.after_llm_response(ctx, response)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "cache")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert chat.attributes[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] == 80
    assert chat.attributes[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] == 20


@pytest.mark.asyncio
async def test_chat_span_records_request_model(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("model_attr", store)
    hook = _make_hook(model="deepseek-chat")

    await hook.before_turn(ctx)
    await hook.after_llm_response(ctx, LLMResponse(content="hi"))
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "model_attr")
    chat = next(s for s in spans if s.name == SpanName.CHAT.value)
    assert chat.attributes[GenAiAttr.REQUEST_MODEL] == "deepseek-chat"


# -- New attributes: tool success/fail/error_type -----------------------------


@pytest.mark.asyncio
async def test_tool_span_success_attribute(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("tool_ok", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    tool_calls = [ToolCall(call_id="c1", tool_name="search", arguments={"q": "x"})]
    await hook.before_tool_execution(ctx, tool_calls)
    results = [ToolResult.from_text("search", "found", execution_time=0.01)]
    await hook.after_tool_execution(ctx, results)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "tool_ok")
    tool_span = next(s for s in spans if s.name == SpanName.EXECUTE_TOOL.value)
    assert tool_span.attributes[GenAiAttr.TOOL_SUCCESS] is True
    assert GenAiAttr.TOOL_ERROR_TYPE not in tool_span.attributes


@pytest.mark.asyncio
async def test_tool_span_fail_and_error_type_attribute(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("tool_fail", store)
    hook = _make_hook()

    await hook.before_turn(ctx)
    tool_calls = [ToolCall(call_id="c1", tool_name="write", arguments={"path": "/x"})]
    await hook.before_tool_execution(ctx, tool_calls)
    results = [ToolResult(tool_name="write", error="permission denied", execution_time=0.01)]
    await hook.after_tool_execution(ctx, results)
    await hook.finally_turn(ctx, None)

    spans = await _collect_spans(store, "tool_fail")
    tool_span = next(s for s in spans if s.name == SpanName.EXECUTE_TOOL.value)
    assert tool_span.attributes[GenAiAttr.TOOL_SUCCESS] is False
    assert tool_span.attributes[GenAiAttr.TOOL_ERROR_TYPE] == "permission denied"
    assert tool_span.status.code == SpanStatusCode.ERROR


# -- New attributes: execute_tool_batch end_time ------------------------------


# -- build_prompt_capture factory ---------------------------------------------


def test_build_prompt_capture_summary() -> None:
    strategy = build_prompt_capture("summary")
    assert isinstance(strategy, SummaryPromptCapture)


def test_build_prompt_capture_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown prompt_capture"):
        build_prompt_capture("nonexistent")


# -- cleanup in finally_turn --------------------------------------------------


@pytest.mark.asyncio
async def test_finally_turn_cleans_up_all_per_trace_state(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    ctx = _make_trace_context("cleanup", store)
    hook = _make_hook(prompt_capture=SummaryPromptCapture(), model="m")

    await hook.before_turn(ctx)
    await hook.before_llm(ctx, [ChatMessage(role=MessageRole.USER, content="hi")])
    _set_iteration(ctx, 1)
    await hook.before_iteration(ctx)
    await hook.before_tool_execution(ctx, [ToolCall(call_id="c1", tool_name="t", arguments={})])
    await hook.finally_turn(ctx, None)

    assert len(hook._llm_start_times) == 0
    assert len(hook._llm_request_attrs) == 0
    assert len(hook._iteration_start_times) == 0
    assert len(hook._tool_batch_info) == 0
    assert len(hook._root_span_info) == 0
