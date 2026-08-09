"""Tests for modex_agent.providers.openai_provider."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("openai")  # skip if openai not installed (CI [dev] doesn't include [llm] deps)

from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.llm_struct import (
    LLMErrorKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.message import ChatMessage, ContentFormat
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.providers.openai_provider import OpenAIProvider
from modex_agent.providers.shared.constants import PROMPT_CACHE_KEY_PARAM, REASONING_EFFORT_PARAM


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

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                                 "cache_read_input_tokens": 1, "reasoning_tokens": 1}

    @pytest.mark.asyncio
    async def test_chat_extracts_cache_from_final_usage_chunk(self, provider):
        """The final chunk (empty choices) carries correct cache tokens.

        Some providers (e.g. stepfun) send usage on EVERY chunk with
        cached_tokens=0, then the final chunk (choices=[]) has the
        correct cached_tokens value. The provider must not skip it.
        """
        # Chunks with choices + usage (cached=0, like stepfun)
        content_chunks = [
            self._make_chunk(content="hi", usage=MagicMock(
                prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
                prompt_tokens_details=MagicMock(cached_tokens=0),
                completion_tokens_details=MagicMock(reasoning_tokens=0),
            )),
            self._make_chunk(content=" there", finish_reason="stop", usage=MagicMock(
                prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
                prompt_tokens_details=MagicMock(cached_tokens=0),
                completion_tokens_details=MagicMock(reasoning_tokens=0),
            )),
        ]
        # Final chunk: NO choices, but usage with correct cache tokens
        final_chunk = MagicMock()
        final_chunk.choices = []
        final_chunk.usage = MagicMock(
            prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
            prompt_tokens_details=MagicMock(cached_tokens=800),
            completion_tokens_details=MagicMock(reasoning_tokens=30),
        )

        all_chunks = content_chunks + [final_chunk]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(all_chunks)
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert result.usage.get("cache_read_input_tokens") == 800, (
            f"Expected 800, got {result.usage.get('cache_read_input_tokens')} — "
            "final usage chunk with empty choices was skipped"
        )
        assert result.usage.get("reasoning_tokens") == 30

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

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="search")])

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

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="?")])
        assert result.reasoning_content == "step by step..."

    @pytest.mark.asyncio
    async def test_chat_error_returns_error_response(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

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
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            model="gpt-4o-mini",
            temperature=0.3,
            max_output_tokens=500,
            tools=[{"type": "function", "function": {"name": "t1", "parameters": {}}}],
        )

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"
        assert call_kwargs["temperature"] == 0.3
        assert call_kwargs["max_tokens"] == 500
        assert len(call_kwargs["tools"]) == 1
        # chat() now uses streaming internally for cache benefits
        assert call_kwargs["stream"] is True


class TestOpenAIProviderReasoningEffort:
    """OpenAIProvider reasoning_effort parameter tests."""

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

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(
                model="gpt-4o",
                api_key="sk-test",
                reasoning_effort=ReasoningEffort.MEDIUM,
                safety=safety,
            )
            p._client = mock_client
            yield p

    @pytest.mark.asyncio
    async def test_reasoning_effort_passed_when_non_none(self, provider):
        chunks = [self._make_chunk(content="ok", finish_reason="stop")]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )
        await provider.chat(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        assert provider._client.chat.completions.create.call_args.kwargs[REASONING_EFFORT_PARAM] == ReasoningEffort.MEDIUM.value

    def test_reasoning_effort_omitted_when_none(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("modex_agent.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(
                model="gpt-4o",
                api_key="sk-test",
                reasoning_effort=ReasoningEffort.NONE,
                safety=safety,
            )
            p._client = mock_client
            params = p._build_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
            assert REASONING_EFFORT_PARAM not in params


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
            ChatMessage(
                role=MessageRole.SYSTEM,
                content="sys",
                content_format=ContentFormat.XML,
                truncatable_paths=["content"],
            ),
            ChatMessage(role=MessageRole.USER, content="hi"),
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert "content_format" not in api_msgs[0]
        assert "truncatable_paths" not in api_msgs[0]
        assert api_msgs[0]["role"] == "system"
        assert api_msgs[0]["content"] == "sys"

    def test_strips_metadata_and_lossy_fields(self, provider):
        # Extras (metadata / meta_* governance fields) ride on ChatMessage
        # via extra="allow"; _build_params must strip them before the API call.
        messages = [
            ChatMessage.from_dict(
                {
                    "role": "assistant",
                    "content": "reply",
                    "metadata": {"source": "urb"},
                    "meta_context_lossy": True,
                    "meta_original_chars": 5000,
                    "meta_context_reduction": "content_truncated",
                }
            ),
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
            ChatMessage(role=MessageRole.USER, content="hi", name="user1"),
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=None,
                tool_calls=[ToolCall(tool_name="t", arguments={}, call_id="c1")],
            ),
            ChatMessage(role=MessageRole.TOOL, tool_call_id="c1", content="result"),
        ]
        params = provider._build_params(messages=messages)
        api_msgs = params["messages"]

        assert api_msgs[0] == {"role": "user", "content": "hi", "name": "user1"}
        assert api_msgs[1]["tool_calls"] is not None
        assert api_msgs[1]["tool_calls"][0] == {
            "id": "c1",
            "type": "function",
            "function": {"name": "t", "arguments": "{}"},
        }
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
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
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
            messages=[ChatMessage(role=MessageRole.USER, content="?")],
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
            messages=[ChatMessage(role=MessageRole.USER, content="search")],
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
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
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
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )

        assert result.content == "data"

    @pytest.mark.asyncio
    async def test_chat_stream_error(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
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
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
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


class TestOpenAIProviderCacheControl:
    """OpenAIProvider prompt_cache_key parameter tests.

    Verifies that ``prompt_cache_key`` passed via kwargs is injected into
    the API request params via ``inject_cache_control``, following the
    same pattern as ``reasoning_effort``.
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

    def test_prompt_cache_key_injected_from_kwargs(self, provider):
        """_build_params pops prompt_cache_key from kwargs and injects it."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="test-session-123",
        )
        assert params[PROMPT_CACHE_KEY_PARAM] == "test-session-123"

    def test_prompt_cache_key_omitted_when_not_in_kwargs(self, provider):
        """No prompt_cache_key in kwargs → param absent."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        assert PROMPT_CACHE_KEY_PARAM not in params

    def test_prompt_cache_key_omitted_when_empty(self, provider):
        """Empty session_id → param absent (inject_cache_control guard)."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="",
        )
        assert PROMPT_CACHE_KEY_PARAM not in params

    def test_prompt_cache_key_not_duplicated_in_kwargs(self, provider):
        """prompt_cache_key is popped from kwargs, not merged twice by params.update."""
        params = provider._build_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="session-abc",
        )
        assert list(params.keys()).count(PROMPT_CACHE_KEY_PARAM) == 1

    @pytest.mark.asyncio
    async def test_prompt_cache_key_flows_to_api_call(self, provider):
        """End-to-end: chat_stream passes prompt_cache_key to the SDK call."""
        chunks = [self._make_chunk(content="ok", finish_reason="stop")]
        provider._client.chat.completions.create = AsyncMock(
            return_value=self._stream_chunks(chunks)
        )

        async def _on_content(d: str) -> None:
            pass

        async def _on_reasoning(d: str) -> None:
            pass

        await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            on_content_delta=_on_content,
            on_reasoning_delta=_on_reasoning,
            prompt_cache_key="e2e-session-id",
        )

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs[PROMPT_CACHE_KEY_PARAM] == "e2e-session-id"

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
