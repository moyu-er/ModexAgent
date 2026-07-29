"""Tests for LiteLLMProvider streaming usage extraction.

Verifies usage (including cache tokens) is correctly extracted from the
final streaming chunk, even when that chunk also carries finish_reason.
"""

from unittest.mock import AsyncMock, patch

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.providers.litellm_provider import LiteLLMProvider


class MockDelta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class MockChoice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class MockUsage:
    def __init__(self, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                 cached_tokens=None, reasoning_tokens=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens
        if cached_tokens is not None:
            self.prompt_tokens_details = type("PD", (), {"cached_tokens": cached_tokens})()
        else:
            self.prompt_tokens_details = None
        if reasoning_tokens is not None:
            self.completion_tokens_details = type("CD", (), {"reasoning_tokens": reasoning_tokens})()
        else:
            self.completion_tokens_details = None


class MockChunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class TestLiteLLMProviderUsageExtraction:
    """LiteLLMProvider streaming usage extraction tests."""

    @pytest.fixture
    def provider(self):
        with patch.dict("os.environ", {"LITELLM_LOG": "ERROR"}):
            provider = LiteLLMProvider(model="test-model", api_key="test-key")
            provider._acompletion = AsyncMock()
            return provider

    @pytest.mark.asyncio
    async def test_usage_with_cache_on_final_chunk_with_finish_reason(self, provider):
        """Usage must be extracted even when the final chunk has both
        finish_reason and usage (not just empty-choices chunks)."""
        chunks = [
            MockChunk(
                choices=[MockChoice(MockDelta(content="hello"))],
                usage=None,
            ),
            MockChunk(
                choices=[MockChoice(MockDelta(), finish_reason="stop")],
                usage=MockUsage(
                    prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
                    cached_tokens=800, reasoning_tokens=30,
                ),
            ),
        ]

        async def mock_stream():
            for c in chunks:
                yield c

        provider._acompletion.return_value = mock_stream()

        result = await provider.chat_stream(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )

        assert result.usage.get("cache_read_input_tokens") == 800, (
            f"Expected 800, got {result.usage.get('cache_read_input_tokens')} — "
            "usage chunk with finish_reason was skipped"
        )
        assert result.usage.get("reasoning_tokens") == 30
        assert result.usage.get("prompt_tokens") == 1000
