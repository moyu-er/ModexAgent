"""Integration test: OpenAIProvider end-to-end with real API credentials.

Validates: non-streaming, streaming, tool calling, factory routing.
Requires: .env file in examples/bot_project/ (or env vars set directly).

Run: pytest tests/integration/test_openai_provider.py -v -m integration
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
    """Load .env from bot_project. Skips test if vars missing."""
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
    """Read LLM config from env, return None if not available."""
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


class TestOpenAIProviderNonStreaming:
    """End-to-end non-streaming chat tests with real API."""

    @pytest.mark.asyncio
    async def test_simple_chat(self, provider):
        result = await provider.chat(
            messages=[{"role": "user", "content": "Say exactly: hello world"}],
        )
        assert result.content is not None
        assert "hello" in result.content.lower()
        assert result.finish_reason == "stop"
        assert result.usage.get("total_tokens", 0) > 0

    @pytest.mark.asyncio
    async def test_tool_calling(self, provider):
        tools = [{
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "City name"},
                    },
                    "required": ["city"],
                },
            },
        }]
        result = await provider.chat(
            messages=[{"role": "user", "content": "What's the weather in Beijing? Use the tool."}],
            tools=tools,
        )
        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "get_weather"
        assert "Beijing" in str(result.tool_calls[0].arguments)

    @pytest.mark.asyncio
    async def test_model_name_strips_openai_prefix(self, config):
        """Factory routes openai/ prefix to OpenAIProvider with stripped model name."""
        from framework.providers.openai_provider import OpenAIProvider

        provider = create_llm_provider(config)
        assert isinstance(provider, OpenAIProvider)
        # Model name should NOT contain the prefix
        assert "openai/" not in provider.get_default_model()


class TestOpenAIProviderStreaming:
    """End-to-end streaming chat tests."""

    @pytest.fixture
    def provider(self, config):
        return create_llm_provider(config)

    @pytest.mark.asyncio
    async def test_streaming_chat(self, provider):
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
        assert len(deltas) > 0  # streaming produced deltas

    @pytest.mark.asyncio
    async def test_streaming_tool_calling(self, provider):
        tools = [{
            "type": "function",
            "function": {
                "name": "calculator",
                "description": "Calculate an expression",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {"type": "string", "description": "Math expression"},
                    },
                    "required": ["expression"],
                },
            },
        }]
        deltas: list[str] = []

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "Calculate 2+3 using the calculator tool."}],
            tools=tools,
            on_content_delta=lambda d: deltas.append(d),
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "calculator"


class TestFactoryRouting:
    """Tests that the factory correctly routes model prefixes."""

    def test_openai_prefix_routes_to_openai_provider(self):
        config = LLMConfig(
            model="openai/gpt-4o",
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
        )
        from framework.providers.openai_provider import OpenAIProvider
        provider = create_llm_provider(config)
        assert isinstance(provider, OpenAIProvider)
        assert provider.get_default_model() == "gpt-4o"

    def test_no_prefix_routes_to_litellm(self):
        config = LLMConfig(
            model="gpt-4o",
            api_key="sk-test",
        )
        from framework.providers.litellm_provider import LiteLLMProvider
        provider = create_llm_provider(config)
        assert isinstance(provider, LiteLLMProvider)
        assert provider.get_default_model() == "gpt-4o"
