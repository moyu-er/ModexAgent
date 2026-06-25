"""Tests for modex_agent.providers.openai_provider."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_struct import (
    LLMErrorKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.types import LLMResponse
from modex_agent.providers.openai_provider import OpenAIProvider


class TestOpenAIProviderChat:
    """Unit tests for OpenAIProvider.chat().

    chat() now delegates to chat_stream_with_retry internally (stream=True)
    for prompt cache benefits, while returning a complete LLMResponse to the caller.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
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
    async def test_chat_returns_content(self, provider):
        chunks = [
            self._make_chunk(content="Hello"),
            self._make_chunk(content=", world!", finish_reason="stop",
                             usage=MagicMock(prompt_tokens=100, completion_tokens=50, total_tokens=150)),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        result = await provider.chat(messages=[{"role": "user", "content": "hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self, provider):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "test"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "c1"
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

        result = await provider.chat(messages=[{"role": "user", "content": "search"}])

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "test"}
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_with_reasoning(self, provider):
        chunks = [
            self._make_chunk(reasoning="step by step..."),
            self._make_chunk(content="answer", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
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
        chunks = [self._make_chunk(content="ok", finish_reason="stop")]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
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
        # chat() now uses streaming internally for cache benefits
        assert call_kwargs["stream"] is True


class TestBuildParamsStripsGovernanceFields:
    """_build_params must strip governance-internal fields from messages.

    Fields like content_format, truncatable_paths, metadata, meta_context_lossy
    are internal governance metadata that must never reach external APIs.
    """

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def test_strips_content_format_and_truncatable_paths(self, provider):
        messages = [
            {"role": "system", "content": "sys", "content_format": "xml", "truncatable_paths": ["content"]},
            {"role": "user", "content": "hi"},
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert "content_format" not in api_msgs[0]
        assert "truncatable_paths" not in api_msgs[0]
        assert api_msgs[0]["role"] == "system"
        assert api_msgs[0]["content"] == "sys"

    def test_strips_metadata_and_lossy_fields(self, provider):
        messages = [
            {"role": "assistant", "content": "reply", "metadata": {"source": "urb"}, "meta_context_lossy": True, "meta_original_chars": 5000, "meta_context_reduction": "content_truncated"},
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert "metadata" not in api_msgs[0]
        assert "meta_context_lossy" not in api_msgs[0]
        assert "meta_original_chars" not in api_msgs[0]
        assert "meta_context_reduction" not in api_msgs[0]
        assert api_msgs[0]["role"] == "assistant"
        assert api_msgs[0]["content"] == "reply"

    def test_preserves_standard_api_fields(self, provider):
        messages = [
            {"role": "user", "content": "hi", "name": "user1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert api_msgs[0] == {"role": "user", "content": "hi", "name": "user1"}
        assert api_msgs[1]["tool_calls"] is not None
        assert api_msgs[2]["tool_call_id"] == "c1"


class TestOpenAIProviderChatStream:
    """Unit tests for OpenAIProvider.chat_stream()."""

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=0.1),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
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

    @pytest.mark.asyncio
    async def test_chat_stream_mid_stream_apierror_returns_error_response(self, provider):
        """A mid-stream APIError (e.g. GLM content moderation ``new_sensitive``)
        must be converted into a graceful error LLMResponse, not raised.

        Regression for the crash where the streaming iteration loop only caught
        StopAsyncIteration / TimeoutError, so any mid-stream exception escaped
        and aborted the whole agent turn.
        """
        from openai import APIError

        async def _stream_that_breaks_midway():
            yield self._make_chunk(content="partial answer")
            raise APIError(
                "output new_sensitive (1027)",
                request=MagicMock(),
                body=None,
            )

        provider._client.chat.completions.create = AsyncMock(
            return_value=_stream_that_breaks_midway()
        )

        deltas = []
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_content_delta=lambda d: deltas.append(d),
        )

        # Must not raise; must return a structured error response.
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        # Content-moderation error is classified distinctly and non-retryable.
        assert result.error_info.kind == LLMErrorKind.CONTENT_FILTER
        assert result.error_info.should_retry is False
        # Partial content already streamed before the error must be preserved.
        assert deltas == ["partial answer"]
        assert "new_sensitive" in (result.error or "")
