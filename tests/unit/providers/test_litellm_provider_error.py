"""Tests for LiteLLMProvider error response and stream idle timeout handling.

chat() now delegates to chat_stream_with_retry internally (stream=True),
so all error handling tests mock streaming responses.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_struct import LLMErrorKind
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import LLMResponse, MessageRole


def _make_stream_chunk(content=None, reasoning_content=None, finish_reason=None):
    """Build a single mock chunk for a streaming response.

    Uses plain classes (not MagicMock) so _get_attr_or_extra works correctly.
    """

    class FakeDelta:
        pass

    delta = FakeDelta()
    delta.content = content
    delta.reasoning_content = reasoning_content
    delta.tool_calls = None

    class FakeChoice:
        pass

    choice = FakeChoice()
    choice.delta = delta
    choice.finish_reason = finish_reason

    class FakeChunk:
        pass

    chunk = FakeChunk()
    chunk.choices = [choice]
    return chunk


def _make_stream(chunks):
    """Return a coroutine that yields an async iterator over chunks."""

    class _Iter:
        async def __aiter__(self):
            for c in chunks:
                yield c

    async def _coro(**_kw):
        return _Iter()

    return _coro


class TestChatErrorHandling:
    """chat() error handling via internal streaming path.

    chat() now delegates to chat_stream_with_retry, so errors come through
    the streaming pipeline.
    """

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            p = LiteLLMProvider(model="gpt-4", api_key="test-key")
            p._acompletion = AsyncMock()
            return p

    @pytest.mark.asyncio
    async def test_exception_returns_error_response(self, provider):
        provider._acompletion.side_effect = Exception("Request timed out")

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error is not None
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.TIMEOUT

    @pytest.mark.asyncio
    async def test_cancelled_error_passthrough(self, provider):
        provider._acompletion.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await provider.chat_with_retry(
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
                max_retries=0,
            )

    @pytest.mark.asyncio
    async def test_empty_content_chunk_then_content_chunk(self, provider):
        """Chunks with None content are skipped; final chunk with content wins."""
        chunks = [
            _make_stream_chunk(),  # None content
            _make_stream_chunk(content="Hello world", finish_reason="stop"),
        ]
        provider._acompletion = _make_stream(chunks)

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello world"
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_normal_response_succeeds(self, provider):
        chunks = [
            _make_stream_chunk(content="Hello world", finish_reason="stop"),
        ]
        provider._acompletion = _make_stream(chunks)

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello world"
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_auth_error_not_retryable(self, provider):
        provider._acompletion.side_effect = Exception("401 unauthorized")

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=2,
        )

        # Auth error should not be retried
        assert provider._acompletion.call_count == 1
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.AUTH
        assert result.error_info.should_retry is False

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self, provider):
        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("429 rate limit")

            class _Iter:
                async def __aiter__(self):
                    yield _make_stream_chunk(content="recovered", finish_reason="stop")

            return _Iter()

        provider._acompletion = mock_acompletion

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=2,
        )

        assert call_count == 2
        assert isinstance(result, LLMResponse)
        assert result.content == "recovered"
        assert result.finish_reason == "stop"


class TestChatStreamRawErrorHandling:
    """_chat_stream_raw() error handling and stream idle timeout."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            p = LiteLLMProvider(
                model="gpt-4", api_key="test-key",
                stream_idle_timeout=0.01,  # short timeout for tests
            )
            return p

    @pytest.mark.asyncio
    async def test_stream_exception_returns_error_response(self, provider):
        provider._acompletion = AsyncMock(side_effect=Exception("connection reset"))

        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None

    @pytest.mark.asyncio
    async def test_stream_cancelled_error_passthrough(self, provider):
        provider._acompletion = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await provider.chat_stream_with_retry(
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
                max_retries=0,
            )

    @pytest.mark.asyncio
    async def test_stream_idle_timeout_returns_error_with_partial_content(self, provider):
        """When stream hangs (no chunks within stream_idle_timeout), returns error response."""
        async def hanging_stream(**kwargs):
            class HangingIterator:
                def __init__(self):
                    self._i = 0

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self._i == 0:
                        self._i += 1
                        return _make_stream_chunk(content="partial")
                    # Hang forever (will trigger stream_idle_timeout)
                    await asyncio.sleep(999)
                    raise StopAsyncIteration

                async def aclose(self):
                    pass

            return HangingIterator()

        provider._acompletion = hanging_stream

        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.TIMEOUT
        assert "partial" in (result.content or "")

    @pytest.mark.asyncio
    async def test_stream_mid_stream_exception_returns_error_response(self, provider):
        """A mid-stream exception must become a graceful error response, not raise.

        Regression for the crash where the streaming loop only caught
        StopAsyncIteration / TimeoutError, so any error raised after the first
        chunk (e.g. content-moderation ``new_sensitive``) aborted the turn.
        """
        provider._stream_idle_timeout = 30  # ensure timeout doesn't mask the error

        async def breaking_stream(**_kwargs):
            class BreakingIter:
                def __init__(self):
                    self._i = 0

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self._i == 0:
                        self._i += 1
                        return _make_stream_chunk(content="partial")
                    raise Exception("output new_sensitive (1027)")

                async def aclose(self):
                    pass

            return BreakingIter()

        provider._acompletion = breaking_stream

        deltas = []
        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
            on_content_delta=lambda d: deltas.append(d),
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.CONTENT_FILTER
        assert result.error_info.should_retry is False
        assert "new_sensitive" in (result.error or "")
        # Partial content streamed before the error must be preserved.
        assert deltas == ["partial"]

    @pytest.mark.asyncio
    async def test_stream_normal_response(self, provider):
        """Normal stream should complete successfully."""
        chunks = [_make_stream_chunk(content="hello", finish_reason="stop")]
        provider._acompletion = _make_stream(chunks)

        deltas = []
        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
            on_content_delta=lambda d: deltas.append(d),
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "hello"
        assert result.finish_reason == "stop"
        assert deltas == ["hello"]


class TestClassifyLitellmContentFilter:
    """classify_litellm_error() recognises content-moderation errors."""

    def test_new_sensitive(self):
        from modex_agent.core.llm_struct import classify_litellm_error

        result = classify_litellm_error(Exception("output new_sensitive (1027)"))
        assert result.kind == LLMErrorKind.CONTENT_FILTER
        assert result.should_retry is False

    def test_content_filter(self):
        from modex_agent.core.llm_struct import classify_litellm_error

        result = classify_litellm_error(Exception("content_filter triggered"))
        assert result.kind == LLMErrorKind.CONTENT_FILTER
        assert result.should_retry is False


class TestBuildRequestParams:
    """_build_request_params() injects safety defaults."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            return LiteLLMProvider(model="gpt-4", api_key="test-key")

    def test_num_retries_not_in_params(self, provider):
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        assert "num_retries" not in params
