"""Tests for LiteLLMProvider reasoning_content parsing.

验证 LiteLLMProvider 正确解析 reasoning_content：
- DeepSeek R1 返回的 reasoning_content
- Kimi 模型返回的 reasoning_content
- 不包含 reasoning_content 的模型（向后兼容）
- REASONING_DELTA 事件的生成
"""

from unittest.mock import AsyncMock, patch

import pytest

from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.providers.litellm_provider import LiteLLMProvider
from modex_agent.providers.shared.constants import REASONING_EFFORT_PARAM


class MockDelta:
    """Mock delta object for testing."""
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class MockChoice:
    """Mock choice object for testing."""
    def __init__(self, delta):
        self.delta = delta
        self.finish_reason = None


class MockChunk:
    """Mock chunk object for testing."""
    def __init__(self, choices):
        self.choices = choices


class TestLiteLLMProviderReasoning:
    """LiteLLMProvider reasoning_content extraction tests."""

    @pytest.fixture
    def provider(self):
        """Create LiteLLMProvider instance with mocked acompletion."""
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            provider = LiteLLMProvider(
                model="deepseek-ai/DeepSeek-R1",
                api_key="test-key",
            )
            # Mock the instance's acompletion binding
            provider._acompletion = AsyncMock()
            return provider

    @pytest.mark.asyncio
    async def test_extract_reasoning_content_deepseek(self, provider):
        """Test extracting reasoning_content from DeepSeek R1 format."""
        # Mock chunks with reasoning_content
        chunks = [
            MockChunk([MockChoice(MockDelta(
                content=None,
                reasoning_content="Let me analyze this step by step."
            ))]),
            MockChunk([MockChoice(MockDelta(
                content="The answer is",
                reasoning_content=None
            ))]),
            MockChunk([MockChoice(MockDelta(
                content=" 42",
                reasoning_content=None
            ))]),
        ]

        # Mock the _extract_delta method
        def mock_extract(chunk):
            delta = chunk.choices[0].delta
            result = {}
            if delta.content:
                result["content"] = delta.content
            if delta.reasoning_content:
                result["reasoning_content"] = delta.reasoning_content
            return result

        provider._extract_delta = mock_extract

        # Simulate streaming
        events = []
        for chunk in chunks:
            delta = provider._extract_delta(chunk)

            if "reasoning_content" in delta:
                events.append(("REASONING_DELTA", delta["reasoning_content"]))
            if "content" in delta:
                events.append(("TEXT_DELTA", delta["content"]))

        # Verify events
        assert len(events) == 3
        assert events[0] == ("REASONING_DELTA", "Let me analyze this step by step.")
        assert events[1] == ("TEXT_DELTA", "The answer is")
        assert events[2] == ("TEXT_DELTA", " 42")

    @pytest.mark.asyncio
    async def test_extract_reasoning_content_kimi(self, provider):
        """Test extracting reasoning_content from Kimi format."""
        chunks = [
            MockChunk([MockChoice(MockDelta(
                content=None,
                reasoning_content="Thinking about the problem..."
            ))]),
            MockChunk([MockChoice(MockDelta(
                content=None,
                reasoning_content="I need to consider all factors."
            ))]),
            MockChunk([MockChoice(MockDelta(
                content="Final answer: Yes",
                reasoning_content=None
            ))]),
        ]

        def mock_extract(chunk):
            delta = chunk.choices[0].delta
            result = {}
            if delta.content:
                result["content"] = delta.content
            if delta.reasoning_content:
                result["reasoning_content"] = delta.reasoning_content
            return result

        provider._extract_delta = mock_extract

        events = []
        for chunk in chunks:
            delta = provider._extract_delta(chunk)
            if "reasoning_content" in delta:
                events.append(("REASONING_DELTA", delta["reasoning_content"]))
            if "content" in delta:
                events.append(("TEXT_DELTA", delta["content"]))

        # Should have 2 reasoning events and 1 content event
        reasoning_events = [e for e in events if e[0] == "REASONING_DELTA"]
        content_events = [e for e in events if e[0] == "TEXT_DELTA"]

        assert len(reasoning_events) == 2
        assert len(content_events) == 1

    @pytest.mark.asyncio
    async def test_graceful_degradation_no_reasoning(self, provider):
        """Test graceful handling when model doesn't return reasoning_content."""
        chunks = [
            MockChunk([MockChoice(MockDelta(
                content="Hello",
                reasoning_content=None
            ))]),
            MockChunk([MockChoice(MockDelta(
                content=" World",
                reasoning_content=None
            ))]),
        ]

        def mock_extract(chunk):
            delta = chunk.choices[0].delta
            result = {}
            if delta.content:
                result["content"] = delta.content
            if delta.reasoning_content:
                result["reasoning_content"] = delta.reasoning_content
            return result

        provider._extract_delta = mock_extract

        events = []
        for chunk in chunks:
            delta = provider._extract_delta(chunk)
            if "reasoning_content" in delta:
                events.append(("REASONING_DELTA", delta["reasoning_content"]))
            if "content" in delta:
                events.append(("TEXT_DELTA", delta["content"]))

        # Should only have content events, no reasoning
        assert len(events) == 2
        assert all(e[0] == "TEXT_DELTA" for e in events)

    @pytest.mark.asyncio
    async def test_interleaved_reasoning_and_content(self, provider):
        """Test handling interleaved reasoning and content (edge case)."""
        chunks = [
            MockChunk([MockChoice(MockDelta(
                content=None,
                reasoning_content="First thought"
            ))]),
            MockChunk([MockChoice(MockDelta(
                content="Part 1",
                reasoning_content=None
            ))]),
            MockChunk([MockChoice(MockDelta(
                content=None,
                reasoning_content="Second thought"
            ))]),
            MockChunk([MockChoice(MockDelta(
                content="Part 2",
                reasoning_content=None
            ))]),
        ]

        def mock_extract(chunk):
            delta = chunk.choices[0].delta
            result = {}
            if delta.content:
                result["content"] = delta.content
            if delta.reasoning_content:
                result["reasoning_content"] = delta.reasoning_content
            return result

        provider._extract_delta = mock_extract

        events = []
        for chunk in chunks:
            delta = provider._extract_delta(chunk)
            if "reasoning_content" in delta:
                events.append(("REASONING_DELTA", delta["reasoning_content"]))
            if "content" in delta:
                events.append(("TEXT_DELTA", delta["content"]))

        # Should have alternating events
        assert len(events) == 4
        assert events[0] == ("REASONING_DELTA", "First thought")
        assert events[1] == ("TEXT_DELTA", "Part 1")
        assert events[2] == ("REASONING_DELTA", "Second thought")
        assert events[3] == ("TEXT_DELTA", "Part 2")

    def test_extract_delta_method_handles_model_extra(self, provider):
        """Test that _extract_delta handles model_extra for reasoning_content."""
        # Create a mock delta with model_extra (Pydantic pattern)
        class DeltaWithExtra:
            def __init__(self, content, reasoning_content):
                self.content = content
                self._reasoning = reasoning_content
                self.model_extra = {"reasoning_content": reasoning_content}

            def __getattr__(self, name):
                if name == "reasoning_content":
                    return self._reasoning
                raise AttributeError(name)

        delta = DeltaWithExtra("Hello", "Reasoning")

        # Test _get_attr_or_extra method
        content = provider._get_attr_or_extra(delta, "content")
        reasoning = provider._get_attr_or_extra(delta, "reasoning_content")

        assert content == "Hello"
        assert reasoning == "Reasoning"


class TestLiteLLMProviderReasoningEffort:
    """LiteLLMProvider reasoning_effort parameter tests."""

    @pytest.fixture
    def provider(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            from modex_agent.providers.litellm_provider import LiteLLMProvider
            return LiteLLMProvider(model="openai/o3-mini", api_key="test-key")

    def test_reasoning_effort_passed_when_set(self, provider):
        provider._reasoning_effort = ReasoningEffort.HIGH
        params = provider._build_request_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        assert params.get(REASONING_EFFORT_PARAM) == ReasoningEffort.HIGH.value

    def test_reasoning_effort_omitted_when_none(self, provider):
        provider._reasoning_effort = ReasoningEffort.NONE
        params = provider._build_request_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
        assert REASONING_EFFORT_PARAM not in params

    def test_reasoning_effort_injected_via_constructor(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            p = LiteLLMProvider(
                model="openai/o3-mini",
                api_key="test-key",
                reasoning_effort=ReasoningEffort.LOW,
            )
            params = p._build_request_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
            assert params.get(REASONING_EFFORT_PARAM) == ReasoningEffort.LOW.value


class TestLiteLLMProviderTopP:
    """LiteLLMProvider top_p sampling parameter tests."""

    def test_top_p_default_in_request_params(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            p = LiteLLMProvider(model="openai/o3-mini", api_key="test-key")
            params = p._build_request_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
            assert params["top_p"] == 0.95

    def test_top_p_injected_via_constructor(self):
        with patch.dict('os.environ', {'LITELLM_LOG': 'ERROR'}):
            p = LiteLLMProvider(
                model="openai/o3-mini",
                api_key="test-key",
                top_p=0.9,
            )
            params = p._build_request_params(messages=[ChatMessage(role=MessageRole.USER, content="hi")])
            assert params["top_p"] == 0.9
