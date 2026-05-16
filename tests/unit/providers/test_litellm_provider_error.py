"""Tests for LiteLLMProvider error response and stream idle timeout handling (P0-a 11.2)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.core.constants import FinishReason
from framework.core.llm_struct import LLMErrorInfo, LLMErrorKind, build_timeout_response
from framework.core.types import LLMResponse


class TestChatRawErrorHandling:
    """_chat_raw() converts exceptions to error responses, passthrough CancelledError."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from framework.providers.litellm_provider import LiteLLMProvider
            p = LiteLLMProvider(model="gpt-4", api_key="test-key")
            p._acompletion = AsyncMock()
            return p

    @pytest.mark.asyncio
    async def test_exception_returns_error_response(self, provider):
        provider._acompletion.side_effect = Exception("Request timed out")

        result = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}],
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
                messages=[{"role": "user", "content": "hi"}],
                max_retries=0,
            )

    @pytest.mark.asyncio
    async def test_empty_response_returns_error(self, provider):
        response = MagicMock()
        response.choices = []
        provider._acompletion.return_value = response

        result = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.UNKNOWN

    @pytest.mark.asyncio
    async def test_empty_message_returns_error(self, provider):
        choice = MagicMock()
        choice.message = None
        response = MagicMock()
        response.choices = [choice]
        provider._acompletion.return_value = response

        result = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value

    @pytest.mark.asyncio
    async def test_normal_response_succeeds(self, provider):
        msg = MagicMock()
        msg.content = "Hello world"
        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        provider._acompletion.return_value = response

        result = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello world"
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_auth_error_not_retryable(self, provider):
        provider._acompletion.side_effect = Exception("401 unauthorized")

        result = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=2,
        )

        # Auth error should not be retried
        assert provider._acompletion.call_count == 1
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.AUTH
        assert result.error_info.should_retry is False


class TestChatStreamRawErrorHandling:
    """_chat_stream_raw() error handling and stream idle timeout."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from framework.providers.litellm_provider import LiteLLMProvider
            p = LiteLLMProvider(
                model="gpt-4", api_key="test-key",
                stream_idle_timeout=0.01,  # short timeout for tests
            )
            return p

    @pytest.mark.asyncio
    async def test_stream_exception_returns_error_response(self, provider):
        provider._acompletion = AsyncMock(side_effect=Exception("connection reset"))

        result = await provider.chat_stream_with_retry(
            messages=[{"role": "user", "content": "hi"}],
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
                messages=[{"role": "user", "content": "hi"}],
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
                        # Return one chunk then hang
                        class FakeDelta:
                            pass
                        delta = FakeDelta()
                        delta.content = "partial"
                        delta.reasoning_content = None
                        delta.tool_calls = None

                        class FakeChoice:
                            def __init__(self):
                                self.delta = delta
                                self.finish_reason = None

                        class FakeChunk:
                            def __init__(self):
                                self.choices = [FakeChoice()]

                        return FakeChunk()
                    # Hang forever (will trigger stream_idle_timeout)
                    await asyncio.sleep(999)
                    raise StopAsyncIteration

                async def aclose(self):
                    pass

            return HangingIterator()

        provider._acompletion = hanging_stream

        result = await provider.chat_stream_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=0,
        )

        assert isinstance(result, LLMResponse)
        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.TIMEOUT
        assert "partial" in (result.content or "")

    @pytest.mark.asyncio
    async def test_stream_normal_response(self, provider):
        """Normal stream should complete successfully."""
        async def normal_stream(**kwargs):
            class NormalIterator:
                def __init__(self):
                    self._done = False

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if self._done:
                        raise StopAsyncIteration
                    self._done = True

                    class FakeDelta:
                        pass
                    delta = FakeDelta()
                    delta.content = "hello"
                    delta.reasoning_content = None
                    delta.tool_calls = None

                    class FakeChoice:
                        def __init__(self):
                            self.delta = delta
                            self.finish_reason = "stop"

                    class FakeChunk:
                        def __init__(self):
                            self.choices = [FakeChoice()]

                    return FakeChunk()

            return NormalIterator()

        provider._acompletion = normal_stream

        deltas = []
        result = await provider.chat_stream_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=0,
            on_content_delta=lambda d: deltas.append(d),
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "hello"
        assert result.finish_reason == "stop"
        assert deltas == ["hello"]


class TestBuildRequestParams:
    """_build_request_params() injects safety defaults."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from framework.providers.litellm_provider import LiteLLMProvider
            return LiteLLMProvider(model="gpt-4", api_key="test-key")

    def test_num_retries_not_in_params(self, provider):
        params = provider._build_request_params(
            messages=[{"role": "user", "content": "hi"}],
        )
        assert "num_retries" not in params
