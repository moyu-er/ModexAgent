"""Tests for modex_agent.providers.shared.constants."""

from __future__ import annotations

from modex_agent.core.constants import ReasoningEffort
from modex_agent.providers.shared.constants import (
    REASONING_EFFORT_PARAM,
    inject_reasoning_effort,
)


def test_inject_reasoning_effort_adds_value_when_non_none() -> None:
    params: dict[str, object] = {}
    inject_reasoning_effort(params, ReasoningEffort.HIGH)
    assert params[REASONING_EFFORT_PARAM] == "high"


def test_inject_reasoning_effort_skips_none() -> None:
    params: dict[str, object] = {}
    inject_reasoning_effort(params, ReasoningEffort.NONE)
    assert REASONING_EFFORT_PARAM not in params


def test_inject_reasoning_effort_does_not_overwrite_existing() -> None:
    params: dict[str, object] = {REASONING_EFFORT_PARAM: "low"}
    inject_reasoning_effort(params, ReasoningEffort.HIGH)
    assert params[REASONING_EFFORT_PARAM] == "low"
