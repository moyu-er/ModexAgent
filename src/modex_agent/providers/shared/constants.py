"""Shared constants across LLM provider implementations."""

from __future__ import annotations

from typing import Any

from modex_agent.core.constants import ReasoningEffort

REASONING_EFFORT_PARAM = "reasoning_effort"

PROMPT_CACHE_KEY_PARAM = "prompt_cache_key"


def inject_reasoning_effort(params: dict[str, Any], reasoning_effort: ReasoningEffort) -> None:
    """Inject the reasoning_effort API parameter when it is configured.

    ``ReasoningEffort.NONE`` and an already-present parameter key are both
    treated as "do not overwrite / do not send".
    """
    if reasoning_effort != ReasoningEffort.NONE and REASONING_EFFORT_PARAM not in params:
        params[REASONING_EFFORT_PARAM] = reasoning_effort.value


def inject_cache_control(params: dict[str, Any], session_id: str) -> None:
    """Inject the prompt_cache_key API parameter for provider-level prefix caching.

    OpenAI/Kimi: ``prompt_cache_key`` enables automatic prefix caching —
    requests with the same key and matching prefix share cached tokens.
    DeepSeek: automatic prefix caching, no explicit parameter needed —
    the key is harmless if the provider ignores it.
    Anthropic: uses ``cache_control`` on content blocks, not a top-level
    key — handled separately by the provider if needed.
    litellm: passes through unknown kwargs to the underlying provider.

    ``session_id`` is a stable per-session identifier, so the same session's
    LLM calls share cache while different sessions do not interfere.

    An empty ``session_id`` and an already-present parameter key are both
    treated as "do not overwrite / do not send".
    """
    if session_id and PROMPT_CACHE_KEY_PARAM not in params:
        params[PROMPT_CACHE_KEY_PARAM] = session_id
