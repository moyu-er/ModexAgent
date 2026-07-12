"""Shared constants across LLM provider implementations."""

from __future__ import annotations

from typing import Any

from modex_agent.core.constants import ReasoningEffort

REASONING_EFFORT_PARAM = "reasoning_effort"


def inject_reasoning_effort(params: dict[str, Any], reasoning_effort: ReasoningEffort) -> None:
    """Inject the reasoning_effort API parameter when it is configured.

    ``ReasoningEffort.NONE`` and an already-present parameter key are both
    treated as "do not overwrite / do not send".
    """
    if reasoning_effort != ReasoningEffort.NONE and REASONING_EFFORT_PARAM not in params:
        params[REASONING_EFFORT_PARAM] = reasoning_effort.value
