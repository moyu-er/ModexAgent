"""Tests for HTTPStreamProvider (providers/http/provider.py).

Every case injects an ``httpx.MockTransport`` — zero real network. Canned
bodies are raw SSE bytes (chat data-only frames, anthropic event+data
frames) that the landed engines already translate, so each test drives the
full provider → engine → assembler chain.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_struct import (
    LLMErrorKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
)
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.types import TokenUsage
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.provider import HTTPStreamProvider

_SSE_HEADERS = {"content-type": "text/event-stream"}

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]

_CHAT_SSE = (
    b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
    b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n'
    b"data: [DONE]\n\n"
)

_ANTHROPIC_SSE = (
    b"event: message_start\n"
    b'data: {"type":"message_start","message":{"role":"assistant","usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
    b"event: content_block_start\n"
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Let me think"}}\n\n'
    b"event: content_block_delta\n"
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"signature_delta","signature":"sig-abc123"}}\n\n'
    b"event: content_block_stop\n"
    b'data: {"type":"content_block_stop","index":0}\n\n'
    b"event: message_delta\n"
    b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
    b"event: message_stop\n"
    b'data: {"type":"message_stop"}\n\n'
)


def _chat_sse_handler() -> Handler:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CHAT_SSE, headers=_SSE_HEADERS)

    return handler


def _user_message() -> list[ChatMessage]:
    return [ChatMessage(role=MessageRole.USER, content="hi")]


@pytest.fixture
async def make_provider() -> AsyncIterator[
    Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]]
]:
    """Build providers on a recording MockTransport; closes every client afterwards."""
    created: list[HTTPStreamProvider] = []

    def _make(handler: Handler, **kwargs: Any) -> tuple[HTTPStreamProvider, list[httpx.Request]]:
        requests: list[httpx.Request] = []

        async def recording(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return await handler(request)

        provider = HTTPStreamProvider(
            model="test-model",
            api_key=kwargs.pop("api_key", "test-key"),
            url=kwargs.pop("url", "https://api.example.com/v1/chat/completions"),
            protocol=kwargs.pop("protocol", None) or OpenAICompatProtocol(),
            transport=httpx.MockTransport(recording),
            **kwargs,
        )
        created.append(provider)
        return provider, requests

    yield _make
    for provider in created:
        await provider.aclose()


async def test_chat_stream_happy_path_assembles_full_response(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    provider, _requests = make_provider(_chat_sse_handler())
    response = await provider.chat_stream(messages=_user_message())

    assert response.content == "Hello world"
    assert response.finish_reason == FinishReason.STOP
    assert response.usage == TokenUsage(input_tokens=10, output_tokens=5)
    assert response.error is None


async def test_non_2xx_response_becomes_classified_error_response(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=(
                b'{"error":{"message":"Incorrect API key provided",'
                b'"type":"invalid_request_error","code":"invalid_api_key"}}'
            ),
            headers={"content-type": "application/json"},
        )

    provider, _requests = make_provider(handler)
    response = await provider.chat_stream(messages=_user_message())

    assert response.finish_reason == FinishReason.ERROR
    assert response.error_info is not None
    assert response.error_info.kind == LLMErrorKind.AUTH
    assert response.error_info.should_retry is False
    assert "Incorrect API key provided" in (response.error or "")


async def test_idle_gap_becomes_timeout_error_response(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    async def slow_body() -> AsyncIterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        await asyncio.sleep(0.2)
        yield b"data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=slow_body(), headers=_SSE_HEADERS)

    safety = RuntimeSafetyPolicy(llm=LLMTimeoutPolicy(stream_idle_timeout_seconds=0.05))
    provider, _requests = make_provider(handler, safety=safety)
    response = await provider.chat_stream(messages=_user_message())

    assert response.finish_reason == FinishReason.ERROR
    assert response.error_info is not None
    assert response.error_info.kind == LLMErrorKind.TIMEOUT
    assert response.error_info.should_retry is True
    assert "idle timeout" in (response.error or "")
    # Text streamed before the idle gap is preserved (assembler splice).
    assert response.content == "partial"


async def test_user_header_override_wins_without_duplicate_headers(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    provider, requests = make_provider(
        _chat_sse_handler(),
        headers={"authorization": "x"},
    )
    await provider.chat_stream(messages=_user_message())

    # One lowercase user entry only — the engine's "Authorization" was
    # replaced, not duplicated into a multi-value header.
    assert requests[0].headers.get_list("authorization") == ["x"]


async def test_provider_requests_ctor_url_verbatim(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    """The provider carries zero URL-construction knowledge: whatever url the
    factory resolved at construction is requested exactly as-is (no join, no
    base-vs-endpoint pick — that logic lives in the factory)."""
    provider, requests = make_provider(
        _chat_sse_handler(), url="https://gateway.example.com/llm"
    )
    await provider.chat_stream(messages=_user_message())

    assert str(requests[0].url) == "https://gateway.example.com/llm"


async def test_anthropic_replay_signature_reaches_response(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ANTHROPIC_SSE, headers=_SSE_HEADERS)

    provider, _requests = make_provider(handler, protocol=AnthropicProtocol())
    response = await provider.chat_stream(messages=_user_message())

    # Finish.replay → assembler chain: signature and final reasoning text
    # land on the response without any provider-side handling.
    assert response.finish_reason == FinishReason.STOP
    assert response.reasoning_content == "Let me think"
    assert response.reasoning_signature == "sig-abc123"


async def test_connection_error_becomes_connection_error_response(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    provider, _requests = make_provider(handler)
    response = await provider.chat_stream(messages=_user_message())

    assert response.finish_reason == FinishReason.ERROR
    assert response.error_info is not None
    assert response.error_info.kind == LLMErrorKind.CONNECTION
    assert response.error_info.should_retry is True


async def test_api_key_falls_back_to_environment_variable(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _chat_sse_handler()

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    provider, requests = make_provider(handler, api_key=None)
    await provider.chat_stream(messages=_user_message())
    assert requests[0].headers["authorization"] == "Bearer env-key"

    monkeypatch.delenv("OPENAI_API_KEY")
    provider2, requests2 = make_provider(handler, api_key=None)
    await provider2.chat_stream(messages=_user_message())
    assert "authorization" not in requests2[0].headers


async def test_aclose_is_idempotent(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    provider, _requests = make_provider(_chat_sse_handler())
    await provider.aclose()
    await provider.aclose()


async def test_provider_sampling_defaults_fill_unset_envelope_fields(
    make_provider: Callable[..., tuple[HTTPStreamProvider, list[httpx.Request]]],
) -> None:
    provider, requests = make_provider(_chat_sse_handler(), temperature=0.2, top_p=0.5)

    await provider.chat_stream(messages=_user_message())
    body = json.loads(requests[0].content)
    assert body["temperature"] == 0.2
    assert body["top_p"] == 0.5

    # Call-site explicit temperature outranks the provider default; top_p
    # has no call-site channel and keeps the provider value.
    await provider.chat_stream(messages=_user_message(), temperature=0.9)
    body = json.loads(requests[1].content)
    assert body["temperature"] == 0.9
    assert body["top_p"] == 0.5
