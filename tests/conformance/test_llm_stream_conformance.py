"""LLM stream conformance (plan §18.3, work package B3).

Both stream implementations — the callback→event bridge
(``CallbackStreamProvider`` with a scripted ``chat_stream``) and the
direct-HTTP path (``HTTPStreamProvider`` + OpenAICompat engine over an
``httpx.MockTransport``) — are driven through the same surface
(``stream(request)`` collected event-by-event, then folded once through
``EventAssembler``) and must satisfy the same contract:

1. every stream ends with exactly one ``Finish`` or ``StreamFailure``;
2. no event arrives after the terminal event (asserted directly on the
   live iterator, not just on a collected list);
3. a stream that runs out without a terminal event folds to a failure
   response (for the HTTP lane the fold synthesizes it; the callback
   bridge structurally cannot end without a terminal — its guarantee);
4. partial content streamed before the failure survives the fold;
5. tool, delta, and usage order is preserved end to end.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any

import httpx
import pytest

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import FinishReason, LLMErrorKind, LLMResponse, TokenUsage
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.provider import CallbackStreamProvider, LLMProvider
from modex_agent.core.stream_events import (
    EventAssembler,
    Finish,
    LLMStreamEvent,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
)
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider

_SSE_HEADERS = {"content-type": "text/event-stream"}

Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]

_TOOLS = [
    ToolCall(call_id="call_a", tool_name="get_weather", arguments={"city": "Paris"}),
    ToolCall(call_id="call_b", tool_name="get_time", arguments={"tz": "UTC"}),
]
_USAGE = TokenUsage(input_tokens=10, output_tokens=5)


def _request() -> LLMRequest:
    return LLMRequest(
        model="conf-model",
        messages=[ChatMessage(role=MessageRole.USER, content="hi")],
    )


async def _collect(provider: LLMProvider) -> list[LLMStreamEvent]:
    return [event async for event in provider.stream(_request())]


async def _fold(events: list[LLMStreamEvent]) -> LLMResponse:
    assembler = EventAssembler()
    for event in events:
        await assembler.feed(event)
    return assembler.result()


def _terminal_indices(events: list[LLMStreamEvent]) -> list[int]:
    return [
        i for i, event in enumerate(events) if isinstance(event, Finish | StreamFailure)
    ]


# ── Callback lane: chat_stream-only override (the standard callback shape) ──


class _ScriptedCallbackProvider(CallbackStreamProvider):
    def __init__(
        self, script: Callable[..., Awaitable[LLMResponse]]
    ) -> None:
        super().__init__()
        self._script = script

    def get_default_model(self) -> str:
        return "scripted-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self._script(on_content_delta)


async def _happy_script(
    on_content_delta: Callable[[str], Any] | None,
) -> LLMResponse:
    if on_content_delta is not None:
        await on_content_delta("Hello")
        await on_content_delta(" world")
    return LLMResponse(
        content="Hello world",
        finish_reason=FinishReason.TOOL_CALLS,
        tool_calls=[ToolCall.model_validate(t.model_dump()) for t in _TOOLS],
        usage=_USAGE,
    )


async def _truncated_script(
    on_content_delta: Callable[[str], Any] | None,
) -> LLMResponse:
    if on_content_delta is not None:
        await on_content_delta("partial")
    raise RuntimeError("wire cut mid-stream")


# ── HTTP lane: OpenAICompat engine over canned SSE bytes ────────────────────


def _happy_sse_handler() -> Handler:
    body = (
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a",'
        b'"type":"function","function":{"name":"get_weather","arguments":""}}]}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_b",'
        b'"type":"function","function":{"name":"get_time","arguments":""}}]}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
        b'{"arguments":"{\\"city\\":\\"Paris\\"}"}}]}}]}\n\n'
        b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"function":'
        b'{"arguments":"{\\"tz\\":\\"UTC\\"}"}}]}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":10,'
        b'"completion_tokens":5,"total_tokens":15}}\n\n'
        b"data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers=_SSE_HEADERS)

    return handler


def _truncated_sse_handler() -> Handler:
    async def body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        # EOF right after the delta: no finish_reason frame, no [DONE].

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers=_SSE_HEADERS)

    return handler


class _HttpLane:
    def __init__(self, register: Callable[[Handler], HTTPStreamProvider]) -> None:
        self._register = register
        # A clean EOF mid-generation reaches the consumer as a stream with
        # no terminal event; EventAssembler synthesizes the failure (the
        # callback bridge instead always emits a terminal itself).
        self.has_terminal_on_truncation = False

    def happy(self) -> HTTPStreamProvider:
        return self._register(_happy_sse_handler())

    def truncated(self) -> HTTPStreamProvider:
        return self._register(_truncated_sse_handler())


class _CallbackLane:
    has_terminal_on_truncation = True

    def happy(self) -> _ScriptedCallbackProvider:
        return _ScriptedCallbackProvider(_happy_script)

    def truncated(self) -> _ScriptedCallbackProvider:
        return _ScriptedCallbackProvider(_truncated_script)


_Lane = "_CallbackLane | _HttpLane"


@pytest.fixture(params=["callback", "http"])
async def lane(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "callback":
        yield _CallbackLane()
        return
    created: list[HTTPStreamProvider] = []

    def register(handler: Handler) -> HTTPStreamProvider:
        provider = HTTPStreamProvider(
            model="conf-model",
            api_key="test-key",
            url="https://api.example.com/v1/chat/completions",
            protocol=OpenAICompatProtocol(),
            transport=httpx.MockTransport(handler),
        )
        created.append(provider)
        return provider

    yield _HttpLane(register)
    for provider in created:
        await provider.aclose()


async def test_happy_stream_ends_with_exactly_one_terminal(lane: Any) -> None:
    events = await _collect(lane.happy())

    terminals = _terminal_indices(events)
    assert len(terminals) == 1
    assert terminals[0] == len(events) - 1


async def test_no_event_arrives_after_terminal(lane: Any) -> None:
    saw_terminal = False
    async for event in lane.happy().stream(_request()):
        if isinstance(event, Finish | StreamFailure):
            assert not saw_terminal
            saw_terminal = True
        else:
            assert not saw_terminal
    assert saw_terminal


async def test_tool_delta_and_usage_order_is_preserved(lane: Any) -> None:
    events = await _collect(lane.happy())

    tools = [event for event in events if isinstance(event, ToolCallComplete)]
    assert [tool.call_id for tool in tools] == ["call_a", "call_b"]
    assert [tool.tool_name for tool in tools] == ["get_weather", "get_time"]
    assert "".join(
        event.text for event in events if isinstance(event, TextDelta)
    ) == "Hello world"

    response = await _fold(events)
    assert response.finish_reason == FinishReason.TOOL_CALLS
    assert response.content == "Hello world"
    assert [tool.call_id for tool in response.tool_calls] == ["call_a", "call_b"]
    assert response.tool_calls[0].arguments == {"city": "Paris"}
    assert response.tool_calls[1].arguments == {"tz": "UTC"}
    assert response.usage == _USAGE
    assert response.error is None


async def test_truncated_stream_folds_to_failure_keeping_partial(lane: Any) -> None:
    events = await _collect(lane.truncated())
    response = await _fold(events)

    assert response.finish_reason == FinishReason.ERROR
    assert response.content == "partial"

    if lane.has_terminal_on_truncation:
        terminals = _terminal_indices(events)
        assert len(terminals) == 1
        assert isinstance(events[terminals[0]], StreamFailure)
        assert "wire cut mid-stream" in (response.error or "")
    else:
        assert not _terminal_indices(events)
        assert response.error_info is not None
        assert response.error_info.kind == LLMErrorKind.TIMEOUT
        assert response.error_info.should_retry is True
