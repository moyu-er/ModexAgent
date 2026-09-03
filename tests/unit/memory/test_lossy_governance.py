"""Tests for ContextBudgetGovernance (formerly LossyContentCompactionGovernance)."""

from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.message import MessageRole
from modex_agent.memory.context_governance import (
    _CLEARED_PLACEHOLDER,
    META_CONTEXT_LOSSY,
    META_CONTEXT_REDUCTION,
    META_ORIGINAL_CHARS,
    ContextBudgetGovernance,
)
from modex_agent.memory.token_estimator import TokenEstimator

_CTX: MagicMock = MagicMock(spec=AgentContext)


class _CharEstimator(TokenEstimator):
    """1 char = 1 token."""

    def estimate_text(self, text: str) -> int:
        return len(text)


def _make_tool_messages(count: int, content_size: int = 5000) -> list[dict]:
    """Build [system] + N×(assistant + tool_result) messages."""
    msgs: list[dict] = [{"role": str(MessageRole.SYSTEM), "content": "sys"}]
    for i in range(count):
        msgs.append({"role": str(MessageRole.ASSISTANT), "content": f"call_{i}"})
        msgs.append({
            "role": str(MessageRole.TOOL),
            "tool_call_id": f"c{i}",
            "name": "search",
            "content": "t" * content_size,
        })
    return msgs


async def test_budget_prunes_tool_results() -> None:
    """Old tool results outside the protect window are replaced with placeholder.

    15 tool results × ~5015 tokens, keep_recent=5 → floor covers j=10..14
    (5×5015=25075 accumulated).  j=9: 30090 > 20000 → window_start=10.
    outside=10 entries, ~50150 > min_gain(15000) → execute.
    Result: 10 pruned, 5 kept (keep_recent only).
    """
    messages = _make_tool_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )

    result = await gov.apply(messages, _CTX)

    tool_msgs = [(i, m) for i, m in enumerate(result) if m.get("role") == str(MessageRole.TOOL)]
    pruned = [(i, m) for i, m in tool_msgs if m["content"] == _CLEARED_PLACEHOLDER]
    intact = [(i, m) for i, m in tool_msgs if m["content"] == "t" * 5000]

    assert len(pruned) == 10
    assert len(intact) == 5  # keep_recent only
    # Pruned are older (lower indices) than intact
    assert max(i for i, _ in pruned) < min(i for i, _ in intact)


async def test_budget_does_not_mutate_input() -> None:
    messages = _make_tool_messages(15, content_size=5000)
    original = [m.get("content") for m in messages]

    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        keep_recent=5,
    )
    result = await gov.apply(messages, _CTX)

    assert messages[0]["content"] == original[0]
    for i in range(2, len(messages), 2):
        assert messages[i]["content"] == original[i]


async def test_budget_zero_mutation_under_threshold() -> None:
    """When total tokens are within budget, nothing changes."""
    messages = _make_tool_messages(3, content_size=100)
    gov = ContextBudgetGovernance(
        max_context_tokens=100_000,
        token_estimator=_CharEstimator(),
    )
    result = await gov.apply(messages, _CTX)

    assert result == messages
    assert result is not messages


async def test_budget_sets_lossy_metadata() -> None:
    messages = _make_tool_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )

    result = await gov.apply(messages, _CTX)

    pruned = [m for m in result if m.get("content") == _CLEARED_PLACEHOLDER]
    assert len(pruned) == 10
    for m in pruned:
        assert m[META_CONTEXT_LOSSY] is True
        assert m[META_ORIGINAL_CHARS] == 5000
        assert m[META_CONTEXT_REDUCTION] == "tool_result_pruned"


async def test_budget_system_messages_never_pruned() -> None:
    messages = _make_tool_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )
    result = await gov.apply(messages, _CTX)

    assert result[0]["role"] == str(MessageRole.SYSTEM)
    assert result[0]["content"] == "sys"


async def test_budget_skips_when_min_gain_not_met() -> None:
    """Not enough replaceable tokens → skip entirely.

    10 tool results × ~5015 tokens, keep_recent=8 → floor covers j=2..9
    (8×5015=40120 accumulated).  j=1: 45135 > 20000 → window_start=2.
    outside=2, 2×5015≈10030 < min_gain(15000) → skip.
    """
    messages = _make_tool_messages(10, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=8,
    )
    result = await gov.apply(messages, _CTX)

    tool_msgs = [m for m in result if m.get("role") == str(MessageRole.TOOL)]
    for m in tool_msgs:
        assert m["content"] == "t" * 5000


async def test_budget_deterministic_across_calls() -> None:
    """Same input → same output."""
    messages = _make_tool_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        keep_recent=5,
    )
    r1 = await gov.apply(messages, _CTX)
    r2 = await gov.apply(messages, _CTX)
    assert r1 == r2
