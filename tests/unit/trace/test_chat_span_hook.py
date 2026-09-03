from __future__ import annotations

import json
from pathlib import Path

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import FinishReason, LLMResponse, MessageRole, ToolCall
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.trace.chat_span_hook import ChatSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.prompt_capture import FullPromptCapture, OffPromptCapture
from modex_agent.trace.semconv import GenAiAttr, LangfuseObservationType, SpanKind, SpanName
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.tools.manager import InMemoryToolManager


def _make_context(*, with_trace: bool = True) -> AgentContext:
    session = SessionInfo(session_id="session.worker", agent_name="worker")
    state = ReActTurnState(
        identity=TurnIdentity(agent_id="worker", session=session, turn_id="turn-1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    if with_trace:
        state.custom[TurnCustomKey.TRACE_ID] = "trace-1"
        state.custom[TurnCustomKey.ROOT_SPAN_ID] = "root-1"
    return AgentContext(
        system_prompt="Be concise.",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


def _make_hook(
    tmp_path: Path,
    session: TraceSessionState,
    *,
    capture_off: bool = False,
) -> tuple[ChatSpanHook, OtelSpanTraceStore]:
    store = OtelSpanTraceStore(base_dir=tmp_path / "traces")
    prompt_capture = OffPromptCapture() if capture_off else FullPromptCapture()
    hook = ChatSpanHook(
        session=session,
        store=store,
        model="test-model",
        provider_name="test-provider",
        request_params={"temperature": 0.2},
        score_injector=None,
        prompt_capture=prompt_capture,
    )
    return hook, store


async def test_chat_span_has_dual_emission(tmp_path: Path) -> None:
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session)
    context = _make_context()
    request = [ChatMessage(role=MessageRole.USER, content="Hello")]

    await hook.before_llm(context, request)
    await hook.after_llm_response(
        context,
        LLMResponse(content="Hi", usage={"input_tokens": 3, "output_tokens": 2}),
    )

    spans = await store.list_by_session("session.worker")
    assert len(spans) == 1
    span = spans[0]
    assert span.name == SpanName.CHAT.value
    assert span.kind == SpanKind.CLIENT.value
    assert span.parent_span_id == "root-1"
    assert (
        json.loads(str(span.attributes[GenAiAttr.GEN_AI_PROMPT]))
        == span.attributes[GenAiAttr.INPUT_MESSAGES]
    )
    assert span.attributes[GenAiAttr.GEN_AI_COMPLETION] == "Hi"
    assert span.attributes[GenAiAttr.OUTPUT_MESSAGES][0]["parts"][0]["content"] == "Hi"
    assert (
        span.attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE]
        == LangfuseObservationType.GENERATION.value
    )


async def test_chat_span_captures_response_details(tmp_path: Path) -> None:
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session)
    context = _make_context()
    request = [ChatMessage(role=MessageRole.USER, content="Hello")]
    response = LLMResponse(
        content="Searching",
        reasoning_content="Need current data",
        tool_calls=[
            ToolCall(
                call_id="call-1",
                tool_name="search",
                arguments={"query": "trace"},
            )
        ],
        finish_reason=FinishReason.TOOL_CALLS,
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 2,
            "reasoning_tokens": 1,
        },
        completion_start_time="2026-07-29T16:00:00.123456+00:00",
    )

    await hook.before_llm(context, request)
    await hook.after_llm_response(context, response)

    spans = await store.list_by_session("session.worker")
    attributes = spans[0].attributes
    assert attributes[GenAiAttr.OUTPUT_TOOL_CALLS] == [
        {"call_id": "call-1", "tool_name": "search", "arguments": '{"query": "trace"}'},
    ]
    # OUTPUT_MESSAGES tool_call parts carry the id in the OTel parts format.
    tool_call_parts = [
        p
        for p in attributes[GenAiAttr.OUTPUT_MESSAGES][0]["parts"]
        if p.get("type") == "tool_call"
    ]
    assert tool_call_parts == [
        {"type": "tool_call", "id": "call-1", "name": "search", "arguments": '{"query": "trace"}'},
    ]
    assert attributes[GenAiAttr.OUTPUT_REASONING_CONTENT] == "Need current data"
    # prompt_tokens(10) includes cached tokens: uncached input = 10 - 4.
    assert attributes[GenAiAttr.USAGE_INPUT_TOKENS] == 6
    assert attributes[GenAiAttr.USAGE_OUTPUT_TOKENS] == 5
    assert attributes[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] == 4
    assert attributes[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] == 2
    assert attributes[GenAiAttr.USAGE_REASONING_TOKENS] == 1
    assert (
        attributes[GenAiAttr.LANGFUSE_OBSERVATION_COMPLETION_START_TIME]
        == "2026-07-29T16:00:00.123456+00:00"
    )


async def test_chat_span_accumulates_usage(tmp_path: Path) -> None:
    session = TraceSessionState()
    hook, _ = _make_hook(tmp_path, session)
    context = _make_context()
    request = [ChatMessage(role=MessageRole.USER, content="Hello")]

    await hook.before_llm(context, request)
    await hook.after_llm_response(
        context,
        LLMResponse(
            content="First",
            usage={
                "input_tokens": 5,
                "output_tokens": 2,
                "cache_read_input_tokens": 3,
            },
        ),
    )
    await hook.before_llm(context, request)
    await hook.after_llm_response(
        context,
        LLMResponse(
            content="Second",
            usage={
                "prompt_tokens": 7,
                "completion_tokens": 4,
                "reasoning_tokens": 2,
            },
        ),
    )

    assert session.turn_usage["trace-1"] == {
        "input_tokens": 12,
        "output_tokens": 6,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 2,
    }
    assert "trace-1" not in session.llm_start_times
    assert "trace-1" not in session.llm_request_attrs


async def test_chat_span_captures_replayed_reasoning_in_input(tmp_path: Path) -> None:
    """Assistant reasoning_content rides prompt capture into input messages (smoke)."""
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session)
    context = _make_context()
    request = [
        ChatMessage(role=MessageRole.USER, content="Search the docs"),
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ToolCall(call_id="call-1", tool_name="search", arguments={"query": "docs"})
            ],
            reasoning_content="User needs docs; call search",
        ),
    ]

    await hook.before_llm(context, request)
    await hook.after_llm_response(context, LLMResponse(content="Done"))

    spans = await store.list_by_session("session.worker")
    attributes = spans[0].attributes
    assistant_parts = attributes[GenAiAttr.INPUT_MESSAGES][1]["parts"]
    assert assistant_parts[0] == {
        "type": "reasoning",
        "content": "User needs docs; call search",
    }


async def test_chat_span_without_prompt_capture(tmp_path: Path) -> None:
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session, capture_off=True)
    context = _make_context()

    await hook.before_llm(
        context,
        [ChatMessage(role=MessageRole.USER, content="Sensitive")],
    )
    await hook.after_llm_response(context, LLMResponse(content="Acknowledged"))

    spans = await store.list_by_session("session.worker")
    attributes = spans[0].attributes
    assert GenAiAttr.INPUT_MESSAGES not in attributes
    assert GenAiAttr.GEN_AI_PROMPT not in attributes
    assert attributes[GenAiAttr.GEN_AI_COMPLETION] == "Acknowledged"


async def test_chat_span_captures_response_id(tmp_path: Path) -> None:
    """When LLMResponse carries response_id, chat span sets gen_ai.response.id."""
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session)
    context = _make_context()
    request = [ChatMessage(role=MessageRole.USER, content="Hello")]

    await hook.before_llm(context, request)
    await hook.after_llm_response(
        context,
        LLMResponse(content="Hi", response_id="chatcmpl-abc123"),
    )

    spans = await store.list_by_session("session.worker")
    attributes = spans[0].attributes
    assert attributes[GenAiAttr.RESPONSE_ID] == "chatcmpl-abc123"


async def test_chat_span_omits_response_id_when_absent(tmp_path: Path) -> None:
    """When LLMResponse has no response_id, chat span does not set gen_ai.response.id."""
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session)
    context = _make_context()
    request = [ChatMessage(role=MessageRole.USER, content="Hello")]

    await hook.before_llm(context, request)
    await hook.after_llm_response(
        context,
        LLMResponse(content="Hi"),
    )

    spans = await store.list_by_session("session.worker")
    attributes = spans[0].attributes
    assert GenAiAttr.RESPONSE_ID not in attributes


async def test_chat_span_not_emitted_when_no_trace_id(tmp_path: Path) -> None:
    session = TraceSessionState()
    hook, store = _make_hook(tmp_path, session)
    context = _make_context(with_trace=False)

    await hook.before_llm(
        context,
        [ChatMessage(role=MessageRole.USER, content="Hello")],
    )
    await hook.after_llm_response(context, LLMResponse(content="Hi"))

    assert await store.list_by_session("session.worker") == []
    assert session.llm_start_times == {}
    assert session.llm_request_attrs == {}
