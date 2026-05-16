"""Integration test: think-tag extraction with real LLM streaming.

Validates:
- Streaming with parse_think_tags=True does not drop content
- Tool calling in streaming mode works correctly
- Multi-turn streaming preserves context
- Fresh extractor per call (no state leak between invocations)

Requires: .env file in tests/integration/ (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL).
Run: pytest tests/integration/test_think_tag_streaming.py -v -m integration
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from framework.ioc.configs.llm import LLMConfig
from framework.ioc.factories.llm import create_llm_provider

pytestmark = pytest.mark.integration


def _load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    if key not in os.environ:
                        os.environ[key] = val


def _get_config() -> LLMConfig | None:
    _load_env()
    model = os.environ.get("LLM_MODEL", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    base_url = os.environ.get("LLM_BASE_URL", "")
    if not api_key:
        return None
    return LLMConfig(
        model=model or "openai/gpt-4o-mini",
        api_key=api_key,
        base_url=base_url,
        temperature=0.1,
        max_tokens=500,
    )


@pytest.fixture(scope="module")
def config():
    cfg = _get_config()
    if cfg is None:
        pytest.skip("LLM_API_KEY not set in environment")
    return cfg


@pytest.fixture(scope="module")
def provider(config):
    return create_llm_provider(config)


# ------------------------------------------------------------------
# Streaming with think extractor active
# ------------------------------------------------------------------


class TestThinkExtractorStreaming:
    """Verify that parse_think_tags=True (default) does not break streaming."""

    @pytest.fixture
    def provider(self, config):
        return create_llm_provider(config)

    @pytest.mark.asyncio
    async def test_streaming_produces_content(self, provider):
        """Streaming content arrives correctly with think extractor active."""
        deltas: list[str] = []
        reasoning_deltas: list[str] = []

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "Count from 1 to 5, one per line."}],
            on_content_delta=lambda d: deltas.append(d),
            on_reasoning_delta=lambda d: reasoning_deltas.append(d),
        )

        full = "".join(deltas)
        assert "1" in full
        assert "5" in full
        assert result.finish_reason == "stop"
        assert len(deltas) > 0

    @pytest.mark.asyncio
    async def test_streaming_short_reply(self, provider):
        """Short reply works correctly."""
        deltas: list[str] = []

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "Say exactly: OK"}],
            on_content_delta=lambda d: deltas.append(d),
        )

        full = "".join(deltas)
        assert "OK" in full
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_two_streaming_calls_same_provider(self, provider):
        """Two consecutive streaming calls with fresh extractor instances."""
        deltas1: list[str] = []
        r1 = await provider.chat_stream(
            messages=[{"role": "user", "content": "Say: hello"}],
            on_content_delta=lambda d: deltas1.append(d),
        )
        assert "hello" in "".join(deltas1).lower()
        assert r1.finish_reason == "stop"

        deltas2: list[str] = []
        r2 = await provider.chat_stream(
            messages=[{"role": "user", "content": "Say: world"}],
            on_content_delta=lambda d: deltas2.append(d),
        )
        assert "world" in "".join(deltas2).lower()
        assert r2.finish_reason == "stop"


# ------------------------------------------------------------------
# Streaming with tool calls
# ------------------------------------------------------------------


class TestThinkExtractorToolCallStreaming:
    """Tool call parsing is independent of think-tag content extraction."""

    @pytest.fixture
    def provider(self, config):
        return create_llm_provider(config)

    @pytest.fixture
    def calculator_tool(self):
        return [{
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "Evaluate a math expression",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Math expression to evaluate",
                        },
                    },
                    "required": ["expression"],
                },
            },
        }]

    @pytest.mark.asyncio
    async def test_streaming_tool_calling(self, provider, calculator_tool):
        """Tool calls are parsed correctly in streaming with think extractor."""
        deltas: list[str] = []

        result = await provider.chat_stream(
            messages=[{
                "role": "user",
                "content": "Calculate 123 + 456 using the calculator tool.",
            }],
            tools=calculator_tool,
            on_content_delta=lambda d: deltas.append(d),
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "calculator"
        assert "123" in str(result.tool_calls[0].arguments)
        assert "456" in str(result.tool_calls[0].arguments)

    @pytest.mark.asyncio
    async def test_multi_turn_with_tools(self, provider, calculator_tool):
        """Two consecutive tool-calling streaming calls work correctly."""
        # First call
        r1 = await provider.chat_stream(
            messages=[{
                "role": "user",
                "content": "Calculate 2+3 with calculator.",
            }],
            tools=calculator_tool,
        )

        assert r1.has_tool_calls
        tool_name = r1.tool_calls[0].tool_name

        # Second call — new extractor instance
        r2 = await provider.chat_stream(
            messages=[{
                "role": "user",
                "content": "Calculate 10*5 with calculator.",
            }],
            tools=calculator_tool,
        )

        assert r2.has_tool_calls
        assert r2.tool_calls[0].tool_name == tool_name


# ------------------------------------------------------------------
# Multi-turn streaming conversation
# ------------------------------------------------------------------


class TestThinkExtractorMultiTurnStreaming:
    """Multi-turn streaming preserves context across turns."""

    @pytest.fixture
    def provider(self, config):
        return create_llm_provider(config)

    @pytest.mark.asyncio
    async def test_multi_turn_conversation(self, provider):
        """Two-turn conversation where second turn references first."""
        # Turn 1
        deltas1: list[str] = []
        r1 = await provider.chat_stream(
            messages=[{"role": "user", "content": "My name is Alice."}],
            on_content_delta=lambda d: deltas1.append(d),
        )
        assert r1.finish_reason == "stop"

        # Turn 2
        deltas2: list[str] = []
        r2 = await provider.chat_stream(
            messages=[
                {"role": "user", "content": "My name is Alice."},
                {"role": "assistant", "content": "Nice to meet you, Alice!"},
                {"role": "user", "content": "What is my name?"},
            ],
            on_content_delta=lambda d: deltas2.append(d),
        )
        full2 = "".join(deltas2)
        assert "Alice" in full2
        assert r2.finish_reason == "stop"


# ------------------------------------------------------------------
# Non-streaming with think extractor
# ------------------------------------------------------------------


class TestThinkExtractorNonStreaming:
    """Non-streaming also has think-tag extraction enabled by default."""

    @pytest.fixture
    def provider(self, config):
        return create_llm_provider(config)

    @pytest.mark.asyncio
    async def test_non_streaming_produces_content(self, provider):
        """Non-streaming content arrives correctly."""
        result = await provider.chat(
            messages=[{"role": "user", "content": "Say exactly: hello world"}],
        )
        assert result.content is not None
        assert "hello" in result.content.lower()
        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_multiple_non_streaming_calls(self, provider):
        """Multiple non-streaming calls do not affect each other."""
        r1 = await provider.chat(
            messages=[{"role": "user", "content": "Say: alpha"}],
        )
        assert "alpha" in r1.content.lower()

        r2 = await provider.chat(
            messages=[{"role": "user", "content": "Say: beta"}],
        )
        assert "beta" in r2.content.lower()
        assert r2.finish_reason == "stop"
