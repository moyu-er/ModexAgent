"""Tests for framework.providers.openai_provider."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.core.constants import FinishReason
from framework.core.llm_struct import (
    LLMErrorInfo,
    LLMErrorKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from framework.core.types import LLMResponse, ToolCall
from framework.providers.openai_provider import OpenAIProvider


def _make_mock_chat_completion(content="hello", tool_calls=None, finish_reason="stop",
                                reasoning_content=None, usage_tokens=(100, 50, 150)):
    """Build a mock ChatCompletion response with the right attributes."""
    tc_list = []
    if tool_calls:
        for tc in tool_calls:
            func = MagicMock()
            func.name = tc["name"]
            func.arguments = tc["arguments"]
            m = MagicMock()
            m.id = tc["id"]
            m.function = func
            tc_list.append(m)

    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tc_list if tc_list else None
    msg.model_extra = {"reasoning_content": reasoning_content} if reasoning_content else None

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason

    usage = MagicMock()
    usage.prompt_tokens = usage_tokens[0]
    usage.completion_tokens = usage_tokens[1]
    usage.total_tokens = usage_tokens[2]

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


class TestOpenAIProviderChat:
    """Unit tests for OpenAIProvider.chat()."""

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("framework.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    @pytest.mark.asyncio
    async def test_chat_returns_content(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(content="Hello, world!")
        )

        result = await provider.chat(messages=[{"role": "user", "content": "hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(
                content=None,
                tool_calls=[{"id": "c1", "name": "search", "arguments": '{"query": "test"}'}],
                finish_reason="tool_calls",
            )
        )

        result = await provider.chat(messages=[{"role": "user", "content": "search"}])

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "test"}
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_with_reasoning(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(
                content="answer", reasoning_content="step by step..."
            )
        )

        result = await provider.chat(messages=[{"role": "user", "content": "?"}])
        assert result.reasoning_content == "step by step..."

    @pytest.mark.asyncio
    async def test_chat_error_returns_error_response(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat(messages=[{"role": "user", "content": "hi"}])

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.CONNECTION

    @pytest.mark.asyncio
    async def test_chat_passes_parameters_correctly(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(content="ok")
        )

        await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=500,
            tools=[{"type": "function", "function": {"name": "t1", "parameters": {}}}],
        )

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"
        assert call_kwargs["temperature"] == 0.3
        assert call_kwargs["max_tokens"] == 500
        assert len(call_kwargs["tools"]) == 1
        assert call_kwargs["stream"] is False


class TestOpenAIProviderChatStream:
    """Unit tests for OpenAIProvider.chat_stream()."""

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=0.1),
            turn=TurnTimeoutPolicy(),
        )
        with patch("framework.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.mark.asyncio
    async def test_chat_stream_content(self, provider):
        chunks = [
            self._make_chunk(content="Hello"),
            self._make_chunk(content=" world"),
            self._make_chunk(content="!", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        deltas = []
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_content_delta=lambda d: deltas.append(d),
        )

        assert result.content == "Hello world!"
        assert result.finish_reason == "stop"
        assert deltas == ["Hello", " world", "!"]

    @pytest.mark.asyncio
    async def test_chat_stream_with_reasoning(self, provider):
        chunks = [
            self._make_chunk(reasoning="let me think..."),
            self._make_chunk(content="42", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        reasoning_parts = []
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "?"}],
            on_reasoning_delta=lambda d: reasoning_parts.append(d),
        )

        assert result.content == "42"
        assert result.reasoning_content == "let me think..."
        assert reasoning_parts == ["let me think..."]

    @pytest.mark.asyncio
    async def test_chat_stream_with_tool_calls(self, provider):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "x"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "call_x"
        tc.function = func

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = None

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks([chunk])
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "search"}],
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "x"}

    @pytest.mark.asyncio
    async def test_chat_stream_idle_timeout(self, provider):
        """When stream produces nothing, returns error response on idle timeout."""

        async def _slow_stream():
            await asyncio.sleep(999)
            if False:
                yield

        provider._client.chat.completions.create = AsyncMock(
            return_value=_slow_stream()
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.TIMEOUT

    @pytest.mark.asyncio
    async def test_chat_stream_handles_empty_choices(self, provider):
        """Chunks with empty choices list should be skipped."""
        chunk_empty = MagicMock()
        chunk_empty.choices = []

        chunk_content = self._make_chunk(content="data", finish_reason="stop")

        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks([chunk_empty, chunk_content])
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.content == "data"

    @pytest.mark.asyncio
    async def test_chat_stream_error(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.CONNECTION
