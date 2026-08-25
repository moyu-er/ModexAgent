"""Tests for LLMProvider retry functionality.

验证 LLMProvider 的重试机制：
- chat_with_retry 在临时错误时自动重试
- chat_stream_with_retry 在流式中途失败时重试整个流
- _is_transient 正确识别常见临时错误（429, 5xx, timeout 等）
- 配额/计费错误不应重试
"""

from unittest.mock import AsyncMock, patch

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.types import LLMResponse, MessageRole


class MockProvider(LLMProvider):
    """Mock LLMProvider for retry tests."""

    def __init__(self):
        super().__init__()
        self.chat_mock = AsyncMock()

    async def chat(self, messages, **kwargs):
        return await self.chat_mock(messages, **kwargs)

    def get_default_model(self):
        return "mock-model"


class MockStreamingProvider(StreamingLLMProvider):
    """Mock StreamingLLMProvider for testing."""

    def __init__(self):
        super().__init__()
        self.chat_mock = AsyncMock()
        self.chat_stream_mock = AsyncMock()

    async def chat(self, messages, **kwargs):
        return await self.chat_mock(messages, **kwargs)

    async def chat_stream(self, messages, on_content_delta=None, on_reasoning_delta=None, **kwargs):
        return await self.chat_stream_mock(messages, on_content_delta=on_content_delta, on_reasoning_delta=on_reasoning_delta, **kwargs)

    def get_default_model(self):
        return "mock-streaming-model"


class TestIsTransient:
    """_is_transient error classification tests."""

    @pytest.mark.parametrize("error_text", [
        "HTTP 429",
        "rate limit exceeded",
        "Request timeout",
        "timed out waiting",
        "connection reset",
        "server error 500",
        "503 Service Unavailable",
        "502 Bad Gateway",
        "504 Gateway Timeout",
        "overloaded",
        "temporarily unavailable",
    ])
    def test_transient_errors(self, error_text):
        assert LLMProvider._is_transient(Exception(error_text)) is True

    @pytest.mark.parametrize("error_text", [
        "insufficient_quota",
        "billing hard limit reached",
        "invalid api key",
        "not found",
        "bad request",
    ])
    def test_non_transient_errors(self, error_text):
        assert LLMProvider._is_transient(Exception(error_text)) is False

    def test_quota_error_overrides_rate_limit(self):
        """Quota errors containing 'rate limit' text should NOT be transient."""
        error = Exception("rate limit: insufficient_quota")
        assert LLMProvider._is_transient(error) is False


class TestChatWithRetry:
    """chat_with_retry non-streaming tests."""

    @pytest.fixture
    def provider(self):
        return MockProvider()

    @pytest.mark.asyncio
    async def test_success_first_attempt(self, provider):
        provider.chat_mock.return_value = LLMResponse(content="success")

        result = await provider.chat_with_retry(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert isinstance(result, LLMResponse)
        assert result.content == "success"
        assert provider.chat_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_success_after_transient_error(self, provider):
        provider.chat_mock.side_effect = [
            Exception("503 Service Unavailable"),
            LLMResponse(content="success"),
        ]

        result = await provider.chat_with_retry(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert isinstance(result, LLMResponse)
        assert result.content == "success"
        assert provider.chat_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self, provider):
        provider.chat_mock.side_effect = [
            Exception("503 Service Unavailable"),
            Exception("503 Service Unavailable"),
            Exception("503 Service Unavailable"),
            Exception("503 Service Unavailable"),
        ]

        with pytest.raises(Exception, match="503 Service Unavailable"):
            await provider.chat_with_retry(
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
                max_retries=3,
            )

        assert provider.chat_mock.call_count == 4

    @pytest.mark.asyncio
    async def test_no_retry_on_non_transient_error(self, provider):
        provider.chat_mock.side_effect = Exception("invalid api key")

        with pytest.raises(Exception, match="invalid api key"):
            await provider.chat_with_retry(messages=[ChatMessage(role=MessageRole.USER, content="hi")])

        assert provider.chat_mock.call_count == 1


class TestChatStreamWithRetry:
    """chat_stream_with_retry streaming tests."""

    @pytest.fixture
    def provider(self):
        return MockStreamingProvider()

    @pytest.mark.asyncio
    async def test_stream_success_first_attempt(self, provider):
        async def mock_chat_stream(*args, on_content_delta=None, **kwargs):
            if on_content_delta:
                await on_content_delta("Hello")
            return LLMResponse(content="Hello")

        provider.chat_stream_mock = mock_chat_stream

        deltas = []

        async def on_delta(d):
            deltas.append(d)

        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            on_content_delta=on_delta,
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello"
        assert deltas == ["Hello"]

    @pytest.mark.asyncio
    async def test_stream_retries_after_transient_failure(self, provider):
        call_count = 0

        async def mock_chat_stream(*args, on_content_delta=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                if on_content_delta:
                    await on_content_delta("Hel")
                raise Exception("503 Service Unavailable")
            if on_content_delta:
                await on_content_delta("Hello")
            return LLMResponse(content="Hello")

        provider.chat_stream_mock = mock_chat_stream

        deltas = []

        async def on_delta(d):
            deltas.append(d)

        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=2,
            on_content_delta=on_delta,
        )

        assert call_count == 2
        assert isinstance(result, LLMResponse)
        assert result.content == "Hello"
        assert deltas == ["Hel", "Hello"]

    @pytest.mark.asyncio
    async def test_stream_max_retries_exhausted(self, provider):
        async def mock_chat_stream(*args, on_content_delta=None, **kwargs):
            if on_content_delta:
                await on_content_delta("x")
            raise Exception("503 Service Unavailable")

        provider.chat_stream_mock = mock_chat_stream

        async def noop_delta(d):
            pass

        with pytest.raises(Exception, match="503 Service Unavailable"):
            await provider.chat_stream_with_retry(
                messages=[ChatMessage(role=MessageRole.USER, content="hi")],
                max_retries=2,
                on_content_delta=noop_delta,
            )


class TestLiteLLMProviderRetryRouting:
    """LiteLLMProvider routes chat/chat_stream through retry wrappers."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            p = LiteLLMProvider(model="gpt-4", api_key="test-key")
            p._acompletion = AsyncMock()
            return p

    @pytest.mark.asyncio
    async def test_chat_routes_through_streaming_retry_wrapper(self, provider):
        """chat() now uses streaming internally; error via stream returns error response."""
        provider._acompletion.side_effect = Exception("503 Service Unavailable")

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )

        # max_retries=0 means no retry; error response returned directly
        assert provider._acompletion.call_count == 1
        assert isinstance(result, LLMResponse)
        assert result.finish_reason == "error"

    @pytest.mark.asyncio
    async def test_chat_retries_on_transient_error(self, provider):
        call_count = 0

        async def mock_acompletion(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("429 rate limit")

            class FakeDelta:
                pass
            delta = FakeDelta()
            delta.content = "recovered"
            delta.reasoning_content = None
            delta.tool_calls = None

            class FakeChoice:
                def __init__(self):
                    self.delta = delta
                    self.finish_reason = "stop"

            class FakeChunk:
                def __init__(self):
                    self.choices = [FakeChoice()]

            class FakeIterator:
                async def __aiter__(self):
                    yield FakeChunk()

            return FakeIterator()

        provider._acompletion = mock_acompletion

        result = await provider.chat_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=2,
        )

        assert call_count == 2
        assert isinstance(result, LLMResponse)
        assert result.content == "recovered"

    @pytest.mark.asyncio
    async def test_chat_stream_routes_through_retry_wrapper(self, provider):
        """chat_stream() should return LLMResponse and invoke callbacks."""
        async def mock_acompletion(*args, **kwargs):
            class FakeDelta:
                def __init__(self, content):
                    self.content = content
                    self.reasoning_content = None
                    self.tool_calls = None

            class FakeChoice:
                def __init__(self, content):
                    self.delta = FakeDelta(content)
                    self.finish_reason = None

            class FakeChunk:
                def __init__(self, content):
                    self.choices = [FakeChoice(content)]

            class FakeIterator:
                async def __aiter__(self):
                    yield FakeChunk("Hello")
                    yield FakeChunk(" World")

            return FakeIterator()

        provider._acompletion = mock_acompletion

        deltas = []
        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            on_content_delta=lambda d: deltas.append(d),
        )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello World"
        assert deltas == ["Hello", " World"]

    @pytest.mark.asyncio
    async def test_chat_stream_retries_entire_stream(self, provider):
        """If stream fails mid-way, entire stream should be retried."""
        call_count = 0

        async def mock_acompletion(*args, **kwargs):
            nonlocal call_count
            call_count += 1

            class FakeDelta:
                def __init__(self, content):
                    self.content = content
                    self.reasoning_content = None
                    self.tool_calls = None

            class FakeChoice:
                def __init__(self, content):
                    self.delta = FakeDelta(content)
                    self.finish_reason = None

            class FakeChunk:
                def __init__(self, content):
                    self.choices = [FakeChoice(content)]

            class FakeIterator:
                async def __aiter__(self):
                    if call_count == 1:
                        yield FakeChunk("Hel")
                        raise Exception("503 overloaded")
                    yield FakeChunk("Hello")
                    yield FakeChunk(" World")

            return FakeIterator()

        provider._acompletion = mock_acompletion

        deltas = []
        result = await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=2,
            on_content_delta=lambda d: deltas.append(d),
        )

        assert call_count == 2
        assert isinstance(result, LLMResponse)
        assert result.content == "Hello World"
        assert deltas == ["Hel", "Hello", " World"]


class TestLiteLLMTemperatureConfigChain:
    """chat_stream_with_retry must fall back to the ctor temperature when the call omits it.

    Regression: chat_stream_with_retry defaulted ``temperature=0.7``, so the
    LiteLLMProvider constructor value (e.g. model.yml ``temperature: 1.0``) was
    unreachable on that path — the explicit 0.7 always won. The default is now
    None: None falls back to the constructor value, an explicit value wins.
    """

    @staticmethod
    def _fake_stream(content="ok"):
        class FakeDelta:
            def __init__(self):
                self.content = content
                self.reasoning_content = None
                self.tool_calls = None

        class FakeChoice:
            def __init__(self):
                self.delta = FakeDelta()
                self.finish_reason = "stop"

        class FakeChunk:
            def __init__(self):
                self.choices = [FakeChoice()]

        class FakeIterator:
            async def __aiter__(self):
                yield FakeChunk()

        return FakeIterator()

    def _make_provider(self, temperature=None):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            if temperature is not None:
                p = LiteLLMProvider(model="gpt-4", api_key="test-key", temperature=temperature)
            else:
                p = LiteLLMProvider(model="gpt-4", api_key="test-key")
        mock = AsyncMock(return_value=self._fake_stream())
        p._acompletion = mock
        return p, mock

    @pytest.mark.asyncio
    async def test_ctor_temperature_reaches_api_when_call_omits_it(self):
        provider, mock = self._make_provider(temperature=1.0)
        await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )
        assert mock.call_args.kwargs["temperature"] == 1.0

    @pytest.mark.asyncio
    async def test_explicit_temperature_overrides_ctor(self):
        provider, mock = self._make_provider(temperature=1.0)
        await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            temperature=0.2,
            max_retries=0,
        )
        assert mock.call_args.kwargs["temperature"] == 0.2

    @pytest.mark.asyncio
    async def test_ctor_default_temperature_preserved(self):
        provider, mock = self._make_provider()  # ctor default DefaultValues.TEMPERATURE (0.7)
        await provider.chat_stream_with_retry(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            max_retries=0,
        )
        assert mock.call_args.kwargs["temperature"] == 0.7
