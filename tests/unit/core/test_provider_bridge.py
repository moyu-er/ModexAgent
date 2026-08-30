"""回调-事件桥接 + 事件流折叠测试（ADR-0046）.

桥接保真三用例（回调路径 / 返回值路径 / tool_calls 保真）是核心验收：
仅覆写 chat_stream 的回调式 mock 经 CallbackStreamProvider.stream() 自动
获得事件流视图；LLMProvider.chat_stream 把事件流折叠回带回调的 LLMResponse。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.core.types import LLMResponse, MessageRole, TokenUsage, ToolCall
from modex_agent.providers.http.assembler import EventAssembler


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role=MessageRole.USER, content="hello")]


def _request(**overrides: Any) -> LLMRequest:
    data: dict[str, Any] = {"model": "req-model", "messages": _messages()}
    data.update(overrides)
    return LLMRequest(**data)


async def _collect(provider: LLMProvider, request: LLMRequest) -> list[LLMStreamEvent]:
    return [event async for event in provider.stream(request)]


async def _fold(events: list[LLMStreamEvent]) -> LLMResponse:
    assembler = EventAssembler()
    for event in events:
        await assembler.feed(event)
    return assembler.result()


class _ChatStreamMock(CallbackStreamProvider):
    """chat_stream-only 覆写——回调式 mock 的标准形态."""

    def __init__(
        self,
        *,
        deltas: tuple[str, ...] = (),
        reasoning_deltas: tuple[str, ...] = (),
        response: LLMResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        super().__init__()
        self._deltas = deltas
        self._reasoning_deltas = reasoning_deltas
        self._response = response if response is not None else LLMResponse(content="")
        self._error = error
        self.chat_stream_calls: list[dict[str, Any]] = []

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.chat_stream_calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "tools": tools,
                "on_content_delta": on_content_delta,
                "on_reasoning_delta": on_reasoning_delta,
                **kwargs,
            }
        )
        if on_content_delta is not None:
            for delta in self._deltas:
                await on_content_delta(delta)
        if on_reasoning_delta is not None:
            for delta in self._reasoning_deltas:
                await on_reasoning_delta(delta)
        if self._error is not None:
            raise self._error
        return self._response

    def get_default_model(self) -> str:
        return "mock-model"


# ─── 用例1：回调路径 ─────────────────────────────────────────────────────────


async def test_bridge_callback_path_emits_deltas_then_finish() -> None:
    """Given mock 打 delta 回调并返回含 content 的 response
    When stream(req)
    Then 事件序列 [TextDelta×n, UsageSnapshot, Finish]，无补译 TextDelta."""
    usage = TokenUsage(input_tokens=10, output_tokens=5)
    provider = _ChatStreamMock(
        deltas=("Hello", " ", "world"),
        response=LLMResponse(
            content="Hello world",
            finish_reason=FinishReason.STOP,
            usage=usage,
        ),
    )
    events = await _collect(provider, _request())
    assert events == [
        TextDelta(text="Hello"),
        TextDelta(text=" "),
        TextDelta(text="world"),
        UsageSnapshot(usage=usage),
        Finish(finish_reason=FinishReason.STOP),
    ]
    assert (await _fold(events)).content == "Hello world"


# ─── 用例2：返回值路径 ───────────────────────────────────────────────────────


async def test_bridge_return_value_path_backfills_payload() -> None:
    """Given mock 不调回调，返回 content/reasoning/tool_calls
    When stream(req)
    Then 补译 TextDelta + ReasoningDelta + ToolCallComplete×n + Finish."""
    provider = _ChatStreamMock(
        response=LLMResponse(
            content="full",
            reasoning_content="because",
            tool_calls=[
                ToolCall(tool_name="get_weather", arguments={"city": "SF"}, call_id="call_1"),
                ToolCall(tool_name="send_email", arguments={}, call_id="call_2"),
            ],
            finish_reason=FinishReason.TOOL_CALLS,
        ),
    )
    events = await _collect(provider, _request())
    assert events == [
        TextDelta(text="full"),
        ReasoningDelta(text="because"),
        ToolCallComplete(call_id="call_1", tool_name="get_weather", arguments={"city": "SF"}),
        ToolCallComplete(call_id="call_2", tool_name="send_email", arguments={}),
        Finish(finish_reason=FinishReason.TOOL_CALLS),
    ]
    result = await _fold(events)
    assert result.content == "full"
    assert result.reasoning_content == "because"
    assert [tc.call_id for tc in result.tool_calls] == ["call_1", "call_2"]
    assert result.tool_calls[0].tool_name == "get_weather"
    assert result.tool_calls[0].arguments == {"city": "SF"}


# ─── 用例3：tool_calls 保真（混合通道） ──────────────────────────────────────


async def test_bridge_mixed_path_preserves_both_channels() -> None:
    """Given mock 既打 content 回调又返回 tool_calls
    When stream(req)
    Then 两条通道都不丢：content 来自回调累积，tool_calls 来自补译."""
    provider = _ChatStreamMock(
        deltas=("par", "tial"),
        response=LLMResponse(
            content="partial",
            tool_calls=[ToolCall(tool_name="search", arguments={"q": "x"}, call_id="c1")],
            finish_reason=FinishReason.TOOL_CALLS,
        ),
    )
    events = await _collect(provider, _request())
    assert events == [
        TextDelta(text="par"),
        TextDelta(text="tial"),
        ToolCallComplete(call_id="c1", tool_name="search", arguments={"q": "x"}),
        Finish(finish_reason=FinishReason.TOOL_CALLS),
    ]
    result = await _fold(events)
    assert result.content == "partial"
    assert result.tool_calls[0].call_id == "c1"
    assert result.tool_calls[0].arguments == {"q": "x"}


# ─── 用例4：异常路径 ─────────────────────────────────────────────────────────


async def test_bridge_exception_terminates_with_stream_failure() -> None:
    """Given chat_stream 抛 ValueError
    When stream(req)
    Then 序列以 StreamFailure 终结，不向上抛."""
    provider = _ChatStreamMock(error=ValueError("boom"))
    events = await _collect(provider, _request())
    assert len(events) == 1
    failure = events[0]
    assert isinstance(failure, StreamFailure)
    assert failure.error_info.kind is LLMErrorKind.UNKNOWN
    assert "boom" in failure.error_info.message
    assert failure.error_info.should_retry is False


async def test_bridge_transient_exception_sets_should_retry() -> None:
    provider = _ChatStreamMock(error=ValueError("connection reset by peer"))
    events = await _collect(provider, _request())
    failure = events[0]
    assert isinstance(failure, StreamFailure)
    assert failure.error_info.should_retry is True


# ─── ERROR 响应路径（partial_content 不翻倍锁定） ────────────────────────────


async def test_bridge_error_response_becomes_stream_failure() -> None:
    """Given chat_stream 返回 finish_reason=ERROR 的 response（含已流出的 partial）
    When stream(req)
    Then 终结为 StreamFailure，fold 后 content 不翻倍、error 语义保留."""
    error_info = LLMErrorInfo(kind=LLMErrorKind.SERVER, message="upstream 500", should_retry=True)
    provider = _ChatStreamMock(
        response=LLMResponse(
            content="partial",
            finish_reason=FinishReason.ERROR,
            error="upstream 500",
            error_info=error_info,
        ),
    )
    events = await _collect(provider, _request())
    assert events == [
        TextDelta(text="partial"),
        StreamFailure(error_info=error_info, partial_content=""),
    ]
    result = await _fold(events)
    assert result.finish_reason is FinishReason.ERROR
    assert result.content == "partial"
    assert result.error == "upstream 500"
    assert result.error_info == error_info


async def test_bridge_error_response_without_error_info_synthesizes() -> None:
    provider = _ChatStreamMock(
        response=LLMResponse(content=None, finish_reason=FinishReason.ERROR, error="kaput"),
    )
    events = await _collect(provider, _request())
    failure = events[-1]
    assert isinstance(failure, StreamFailure)
    assert failure.error_info.kind is LLMErrorKind.UNKNOWN
    assert failure.error_info.message == "kaput"


# ─── 用例7：CancelledError ───────────────────────────────────────────────────


async def test_bridge_cancelled_error_translates_to_finish_cancelled() -> None:
    provider = _ChatStreamMock(error=asyncio.CancelledError())
    events = await _collect(provider, _request())
    assert events == [Finish(finish_reason=FinishReason.CANCELLED)]


# ─── kwargs 面（cassette 键面红线） ──────────────────────────────────────────


async def test_bridge_replicates_client_kwargs_face() -> None:
    """桥接 kwargs 面逐项复刻 llm_client.py:139-147——刻意不传 model=."""
    provider = _ChatStreamMock(response=LLMResponse(content="x"))
    request = _request(
        temperature=0.3,
        max_output_tokens=128,
        tools=({"name": "t", "parameters": {}},),
        prompt_cache_key="sess-1",
    )
    await _collect(provider, request)
    call = provider.chat_stream_calls[0]
    assert call["model"] is None
    assert call["temperature"] == 0.3
    assert call["max_output_tokens"] == 128
    assert call["tools"] == [{"name": "t", "parameters": {}}]
    assert call["prompt_cache_key"] == "sess-1"
    assert call["on_content_delta"] is not None
    assert call["on_reasoning_delta"] is not None


async def test_bridge_normalizes_empty_tools_and_missing_cache_key() -> None:
    provider = _ChatStreamMock(response=LLMResponse(content="x"))
    await _collect(provider, _request(tools=()))
    call = provider.chat_stream_calls[0]
    assert call["tools"] is None
    assert call["prompt_cache_key"] == ""


# ─── 生成器清理（不泄漏后台任务） ────────────────────────────────────────────


async def test_bridge_aclose_cancels_background_task() -> None:
    """Given chat_stream 挂起的 provider
    When 消费者提前 aclose 生成器
    Then 后台任务被取消并结束（chat_stream 的 finally 已执行）."""
    interrupted = False

    class _HangingProvider(_ChatStreamMock):
        async def chat_stream(
            self,
            messages: list[ChatMessage],
            model: str | None = None,
            temperature: float | None = None,
            max_output_tokens: int | None = None,
            tools: list[dict] | None = None,
            on_content_delta: Callable[[str], Any] | None = None,
            on_reasoning_delta: Callable[[str], Any] | None = None,
            **kwargs: Any,
        ) -> LLMResponse:
            nonlocal interrupted
            try:
                if on_content_delta is not None:
                    await on_content_delta("partial")
                await asyncio.Event().wait()
                return LLMResponse(content="partial")
            finally:
                interrupted = True

    provider = _HangingProvider()
    gen = provider.stream(_request())
    first = await gen.__anext__()
    assert first == TextDelta(text="partial")
    await gen.aclose()
    assert interrupted
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


# ─── LLMProvider 事件流折叠 ──────────────────────────────────────────────────


class _RecordingStreamProvider(LLMProvider):
    """最小子类：只实现 stream() + get_default_model()."""

    def __init__(self, events: list[LLMStreamEvent]) -> None:
        super().__init__()
        self._events = events
        self.requests: list[LLMRequest] = []

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.requests.append(request)
        for event in self._events:
            yield event

    def get_default_model(self) -> str:
        return "event-model"


async def test_event_stream_provider_chat_stream_folds_events() -> None:
    """用例5：仅实现 stream() 即获得完整 chat/chat_stream/重试面."""
    usage = TokenUsage(input_tokens=3, output_tokens=7)
    provider = _RecordingStreamProvider(
        [
            ReasoningDelta(text="thinking"),
            TextDelta(text="an"),
            TextDelta(text="swer"),
            ToolCallComplete(call_id="c1", tool_name="search", arguments={"q": "x"}),
            UsageSnapshot(usage=usage),
            Finish(finish_reason=FinishReason.STOP),
        ]
    )
    deltas: list[str] = []
    response = await provider.chat_stream(
        messages=_messages(),
        on_content_delta=deltas.append,
        prompt_cache_key="sess-9",
    )
    assert response.content == "answer"
    assert response.reasoning_content == "thinking"
    assert response.finish_reason is FinishReason.STOP
    assert response.usage == usage
    assert [tc.call_id for tc in response.tool_calls] == ["c1"]
    assert deltas == ["an", "swer"]
    request = provider.requests[0]
    assert request.model == "event-model"
    assert request.prompt_cache_key == "sess-9"
    assert request.messages == _messages()


async def test_event_stream_provider_inherits_chat() -> None:
    provider = _RecordingStreamProvider(
        [TextDelta(text="ok"), Finish(finish_reason=FinishReason.STOP)]
    )
    response = await provider.chat(messages=_messages())
    assert response.content == "ok"
    assert provider.requests[0].model == "event-model"


async def test_event_stream_provider_chat_retries_transparently() -> None:
    """chat() 经内部 chat_stream 重试收敛到同一事件流（原 chat_with_retry 用例）."""
    provider = _RecordingStreamProvider(
        [TextDelta(text="ok"), Finish(finish_reason=FinishReason.STOP)]
    )
    response = await provider.chat(
        messages=_messages(), temperature=0.2, max_output_tokens=64
    )
    assert response.content == "ok"
    request = provider.requests[0]
    assert request.temperature == 0.2
    assert request.max_output_tokens == 64


async def test_event_stream_provider_drops_unknown_kwargs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """用例6：未知 kwargs 记 ERROR 并丢弃，不炸不透传."""
    provider = _RecordingStreamProvider(
        [TextDelta(text="ok"), Finish(finish_reason=FinishReason.STOP)]
    )
    with caplog.at_level(logging.ERROR, logger="modex_agent.core.provider"):
        response = await provider.chat_stream(messages=_messages(), funny_kwarg=1)
    assert response.content == "ok"
    assert "funny_kwarg" in caplog.text
    assert "funny_kwarg" not in provider.requests[0].model_dump()


def test_event_stream_provider_requires_stream_implementation() -> None:
    class _NoStream(LLMProvider):
        def get_default_model(self) -> str:
            return "m"

    with pytest.raises(TypeError):
        _NoStream()
