"""Tests for LiteLLMProvider prompt_cache_key parameter injection.

Verifies that ``prompt_cache_key`` passed via kwargs is injected into
the API request params via ``inject_cache_control``, following the
same pattern as ``reasoning_effort``.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.providers.litellm_provider import LiteLLMProvider
from modex_agent.providers.shared.constants import PROMPT_CACHE_KEY_PARAM


class TestLiteLLMProviderCacheControl:
    """LiteLLMProvider prompt_cache_key parameter tests."""

    @pytest.fixture
    def provider(self):
        with patch.dict("os.environ", {"LITELLM_LOG": "ERROR"}):
            return LiteLLMProvider(model="openai/gpt-4o", api_key="test-key")

    def test_prompt_cache_key_injected_from_kwargs(self, provider):
        """_build_request_params pops prompt_cache_key from kwargs and injects it."""
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="litellm-session-456",
        )
        assert params[PROMPT_CACHE_KEY_PARAM] == "litellm-session-456"

    def test_prompt_cache_key_omitted_when_not_in_kwargs(self, provider):
        """No prompt_cache_key in kwargs -> param absent."""
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
        )
        assert PROMPT_CACHE_KEY_PARAM not in params

    def test_prompt_cache_key_omitted_when_empty(self, provider):
        """Empty session_id -> param absent (inject_cache_control guard)."""
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="",
        )
        assert PROMPT_CACHE_KEY_PARAM not in params

    def test_prompt_cache_key_not_duplicated(self, provider):
        """prompt_cache_key is popped from kwargs, not merged twice via **kwargs spread."""
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="session-xyz",
        )
        assert list(params.keys()).count(PROMPT_CACHE_KEY_PARAM) == 1

    def test_prompt_cache_key_coexists_with_reasoning_effort(self, provider):
        """Both reasoning_effort and prompt_cache_key can be injected together."""
        from modex_agent.core.constants import ReasoningEffort
        from modex_agent.providers.shared.constants import REASONING_EFFORT_PARAM

        provider._reasoning_effort = ReasoningEffort.HIGH
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="combined-session",
        )
        assert params[REASONING_EFFORT_PARAM] == ReasoningEffort.HIGH.value
        assert params[PROMPT_CACHE_KEY_PARAM] == "combined-session"

    def test_other_kwargs_still_merged(self, provider):
        """Other kwargs are still spread into params after popping prompt_cache_key."""
        params = provider._build_request_params(
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            prompt_cache_key="session-1",
            user="test-user",
        )
        assert params[PROMPT_CACHE_KEY_PARAM] == "session-1"
        assert params["user"] == "test-user"
