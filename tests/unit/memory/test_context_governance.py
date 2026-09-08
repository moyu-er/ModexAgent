"""Tests for ContextGovernance implementations."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import AgentContext
from modex_agent.core.message import MessageRole
from modex_agent.memory.context_governance import (
    _CLEARED_PLACEHOLDER,
    META_CONTEXT_LOSSY,
    META_CONTEXT_REDUCTION,
    META_ORIGINAL_CHARS,
    CompositeGovernance,
    ContextBudgetGovernance,
    ContextGovernance,
    ToolChainRepairGovernance,
)
from modex_agent.memory.token_estimator import TokenEstimator

_CTX: Any = MagicMock(spec=AgentContext)


class _CharEstimator(TokenEstimator):
    """1 char = 1 token, for deterministic tests."""

    def estimate_text(self, text: str) -> int:
        return len(text)


# ── ToolChainRepairGovernance ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_chain_repair_drops_orphans():
    """移除无对应 assistant tool_call 的 orphan tool results."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
        {"role": str(MessageRole.TOOL), "tool_call_id": "call_1", "name": "read_file", "content": "orphan"},
        {"role": str(MessageRole.USER), "content": "ok"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages, _CTX)

    assert len(result) == 2
    assert result[0]["role"] == str(MessageRole.ASSISTANT)
    assert result[1]["role"] == str(MessageRole.USER)


@pytest.mark.asyncio
async def test_tool_chain_repair_backfills_incomplete_assistant_in_model_context():
    """In MODEL_VISIBLE_CONTEXT mode, a dangling assistant tool_call is
    backfilled with a placeholder tool result (not removed) so the provider
    sees a well-formed chain."""
    from modex_agent.memory.sanitizer import BACKFILL_LOST_TOOL_CONTENT

    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": str(MessageRole.USER), "content": "next"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages, _CTX)

    assert result == [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {
            "role": str(MessageRole.TOOL),
            "tool_call_id": "call_1",
            "content": BACKFILL_LOST_TOOL_CONTENT,
        },
        {"role": str(MessageRole.USER), "content": "next"},
    ]


@pytest.mark.asyncio
async def test_tool_chain_repair_preserves_complete_chain():
    """完整的 tool-call 链不应被修改."""
    messages = [
        {"role": str(MessageRole.ASSISTANT), "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": str(MessageRole.TOOL), "tool_call_id": "call_1", "name": "read_file", "content": "file content"},
        {"role": str(MessageRole.ASSISTANT), "content": "done"},
    ]
    gov = ToolChainRepairGovernance()
    result = await gov.apply(messages, _CTX)

    assert len(result) == 3
    assert result[0]["role"] == str(MessageRole.ASSISTANT)
    assert result[1]["role"] == str(MessageRole.TOOL)
    assert result[1]["content"] == "file content"
    assert result[2]["role"] == str(MessageRole.ASSISTANT)


@pytest.mark.asyncio
async def test_tool_chain_repair_cleans_up_orphans_in_model_context() -> None:
    messages: list[dict] = [
        {"role": "user", "content": "do something"},
        {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan result"},
        {"role": "user", "content": "next"},
    ]
    result = await ToolChainRepairGovernance().apply(messages, _CTX)
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "do something"
    assert result[1]["role"] == "user"
    assert result[1]["content"] == "next"


@pytest.mark.asyncio
async def test_tool_chain_repair_backfills_last_incomplete_assistant_for_model_visible_context() -> None:
    from modex_agent.memory.sanitizer import BACKFILL_LOST_TOOL_CONTENT

    messages = [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "tool_a"}},
                {"id": "b", "function": {"name": "tool_b"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "a", "content": "result_a"},
        {"role": str(MessageRole.USER), "content": "next"},
    ]

    result = await ToolChainRepairGovernance().apply(messages, _CTX)

    assert result == [
        {"role": str(MessageRole.USER), "content": "start"},
        {
            "role": str(MessageRole.ASSISTANT),
            "content": "",
            "tool_calls": [
                {"id": "a", "function": {"name": "tool_a"}},
                {"id": "b", "function": {"name": "tool_b"}},
            ],
        },
        {"role": str(MessageRole.TOOL), "tool_call_id": "a", "content": "result_a"},
        {
            "role": str(MessageRole.TOOL),
            "tool_call_id": "b",
            "content": BACKFILL_LOST_TOOL_CONTENT,
        },
        {"role": str(MessageRole.USER), "content": "next"},
    ]


# ── CompositeGovernance ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_composite_runs_strategies_in_order():
    """CompositeGovernance 按顺序执行多个策略."""
    class AddTag(ContextGovernance):
        def __init__(self, tag: str) -> None:
            self.tag = tag

        async def apply(
            self, messages: list[dict[str, Any]], ctx: AgentContext,
        ) -> list[dict[str, Any]]:
            result = list(messages)
            result.append({"role": str(MessageRole.USER), "content": self.tag})
            return result

    composite = CompositeGovernance([AddTag("a"), AddTag("b")])
    result = await composite.apply([{"role": str(MessageRole.USER), "content": "base"}], _CTX)

    assert len(result) == 3
    assert result[1]["content"] == "a"
    assert result[2]["content"] == "b"


# ── ContextBudgetGovernance ────────────────────────────────────────────────
#
# Helper: build a message list with N tool results.
# Each tool message with content of C chars → ~C+overhead tokens with _CharEstimator.
# The overhead is ~17 tokens (MESSAGE_OVERHEAD=4 + name + tool_call_id fields).
# For simplicity in tests we use content sizes large enough that the overhead
# is negligible relative to the parameter values.

def _make_messages(tool_count: int, content_size: int = 5000) -> list[dict[str, Any]]:
    """Build [system] + tool_count×(assistant + tool_result)."""
    msgs: list[dict[str, Any]] = [{"role": str(MessageRole.SYSTEM), "content": "sys"}]
    for i in range(tool_count):
        msgs.append({"role": str(MessageRole.ASSISTANT), "content": f"call_{i}"})
        msgs.append({
            "role": str(MessageRole.TOOL),
            "name": "read_file",
            "content": "x" * content_size,
            "tool_call_id": f"c{i}",
        })
    return msgs


@pytest.mark.asyncio
async def test_budget_zero_mutation_under_threshold():
    """Total tokens ≤ threshold → return copy, no modification."""
    messages = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.USER), "content": "hello"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "result", "tool_call_id": "c1"},
    ]
    gov = ContextBudgetGovernance(
        max_context_tokens=10_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
    )
    result = await gov.apply(messages, _CTX)

    assert result == messages
    assert result is not messages  # must be a copy


@pytest.mark.asyncio
async def test_budget_zero_mutation_empty_input():
    """Empty messages → empty list."""
    gov = ContextBudgetGovernance(max_context_tokens=100)
    result = await gov.apply([], _CTX)
    assert result == []


@pytest.mark.asyncio
async def test_budget_prunes_old_tool_results_outside_window():
    """When over threshold, old tool results outside the protect window are
    replaced with the fixed placeholder.

    Semantics:
      keep_recent = structural floor: last N tool results are NEVER pruned.
      protect_tokens = budget cap on TOTAL retained tool-output tokens,
        including the keep_recent tail.  Window walks newest→oldest over
        ALL tool entries; keep_recent entries always kept but their tokens
        still accumulate.  Only entries beyond the floor are pruned when
        the accumulated total exceeds protect_tokens.

    Setup:
      15 tool results × ~5015 tokens each.
      keep_recent=5 → floor covers j=10..14 (5×5015=25075 tokens accumulated).
      j=9 (first beyond floor): accumulated=30089 > protect_tokens(20000)
        → window_start=10, outside=10.
      10×5015≈50150 > min_gain(15000) → execute.
      Result: 10 pruned (all eligible), 5 kept (keep_recent only).
    """
    messages = _make_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,  # threshold = 50000 × 0.60 = 30000
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )
    result = await gov.apply(messages, _CTX)

    tool_indices = [i for i, m in enumerate(result) if m.get("role") == str(MessageRole.TOOL)]
    pruned = [i for i in tool_indices if result[i].get("content") == _CLEARED_PLACEHOLDER]
    intact = [i for i in tool_indices if result[i].get("content") == "x" * 5000]

    assert len(pruned) == 10  # all eligible entries pruned
    assert len(intact) == 5   # keep_recent only
    # Pruned are the oldest (lowest indices)
    assert max(pruned) < min(intact)


@pytest.mark.asyncio
async def test_budget_skips_when_gain_below_minimum():
    """If replaceable tokens < min_gain, skip entirely (zero-mutation).

    Setup: 10 tool results × ~5015 tokens.
      keep_recent=5 → floor covers j=5..9 (5×5015=25075 accumulated).
      j=4: accumulated=30090 > protect_tokens(20000) → window_start=5.
      outside = tool_entries[:5] = 5 entries, 5×5015≈25075 > min_gain(15000) → execute!

    To make it skip: need outside_tokens < min_gain.
    Use keep_recent=8 → floor covers j=2..9 (8×5015=40120 accumulated).
      j=1: accumulated=45135 > 20000 → window_start=2.
      outside = tool_entries[:2] = 2 entries, 2×5015≈10030 < min_gain(15000) → skip.
    """
    messages = _make_messages(10, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=8,
    )
    result = await gov.apply(messages, _CTX)

    tool_indices = [i for i, m in enumerate(result) if m.get("role") == str(MessageRole.TOOL)]
    for i in tool_indices:
        assert result[i]["content"] == "x" * 5000


@pytest.mark.asyncio
async def test_budget_respects_keep_recent():
    """keep_recent floor is honored: last N tool results are NEVER pruned
    even if window math would include them."""
    # Only 3 tool results → keep_recent=10 means none are eligible.
    messages: list[dict[str, Any]] = [
        {"role": str(MessageRole.SYSTEM), "content": "sys"},
        {"role": str(MessageRole.ASSISTANT), "content": "c0"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "x" * 50000, "tool_call_id": "c0"},
        {"role": str(MessageRole.ASSISTANT), "content": "c1"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "y" * 50000, "tool_call_id": "c1"},
        {"role": str(MessageRole.ASSISTANT), "content": "c2"},
        {"role": str(MessageRole.TOOL), "name": "read_file", "content": "z" * 50000, "tool_call_id": "c2"},
    ]
    gov = ContextBudgetGovernance(
        max_context_tokens=10_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=1000,
        min_gain_tokens=100,
        keep_recent=10,
    )
    result = await gov.apply(messages, _CTX)

    # Only 3 tool results → ≤ keep_recent(10) → no pruning.
    tool_indices = [i for i, m in enumerate(result) if m.get("role") == str(MessageRole.TOOL)]
    for i in tool_indices:
        assert result[i]["content"] != _CLEARED_PLACEHOLDER


@pytest.mark.asyncio
async def test_budget_keep_recent_floor_separate_from_window():
    """keep_recent and protect_tokens compose: the keep_recent tail's tokens
    count toward protect_tokens, so the window only covers entries beyond
    the floor whose accumulated total (including floor) still fits.

    Setup: 8 tool results × ~5015 tokens.
      keep_recent=3 → floor covers j=5..7 (3×5015=15045 accumulated).
      j=4: accumulated=20060 ≤ protect_tokens(30000) → keep.
      j=3: accumulated=25075 ≤ 30000 → keep.
      j=2: accumulated=30090 > 30000 → window_start=3.
      outside = tool_entries[:3] = 3 entries, 3×5015≈15045 > min_gain(5000) → execute.
      Result: 3 pruned (oldest), 2 kept (window), 3 kept (keep_recent).
    """
    messages = _make_messages(8, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=30_000,
        min_gain_tokens=5_000,
        keep_recent=3,
    )
    result = await gov.apply(messages, _CTX)

    tool_indices = [i for i, m in enumerate(result) if m.get("role") == str(MessageRole.TOOL)]
    pruned = [i for i in tool_indices if result[i].get("content") == _CLEARED_PLACEHOLDER]
    intact = [i for i in tool_indices if result[i].get("content") == "x" * 5000]

    assert len(pruned) == 3
    assert len(intact) == 5  # 2 window + 3 keep_recent


@pytest.mark.asyncio
async def test_budget_whitelist_tools_protected():
    """Whitelisted tool results are never pruned.

    Setup: 15 tool results, first is whitelisted. keep_recent=5.
      Eligible = 10 non-whitelisted entries (indices 1-9 in tool_entries).
      Window covers some, outside gets pruned — but whitelisted entry at
      message index 2 is untouched.
    """
    messages: list[dict[str, Any]] = [{"role": str(MessageRole.SYSTEM), "content": "sys"}]
    for i in range(15):
        tool_name = "protected_tool" if i == 0 else "read_file"
        messages.append({"role": str(MessageRole.ASSISTANT), "content": f"call_{i}"})
        messages.append({
            "role": str(MessageRole.TOOL),
            "name": tool_name,
            "content": "x" * 5000,
            "tool_call_id": f"c{i}",
        })

    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
        whitelist_tools=frozenset({"protected_tool"}),
    )
    result = await gov.apply(messages, _CTX)

    # The whitelisted tool result (message index 2) should NOT be placeholder.
    assert result[2]["content"] == "x" * 5000
    # But other old tool results should be pruned.
    pruned = [
        i for i, m in enumerate(result)
        if m.get("content") == _CLEARED_PLACEHOLDER
    ]
    assert len(pruned) > 0
    assert 2 not in pruned


@pytest.mark.asyncio
async def test_budget_does_not_mutate_input():
    """Governance must return a new list and not modify the original."""
    messages = _make_messages(15, content_size=5000)
    original_contents = [m.get("content") for m in messages]

    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )
    result = await gov.apply(messages, _CTX)

    assert result is not messages
    assert [m.get("content") for m in messages] == original_contents


@pytest.mark.asyncio
async def test_budget_sets_metadata_on_pruned():
    """Pruned messages carry META_CONTEXT_LOSSY, META_ORIGINAL_CHARS, META_CONTEXT_REDUCTION."""
    messages = _make_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )
    result = await gov.apply(messages, _CTX)

    pruned = [m for m in result if m.get("content") == _CLEARED_PLACEHOLDER]
    assert len(pruned) > 0
    for m in pruned:
        assert m[META_CONTEXT_LOSSY] is True
        assert m[META_ORIGINAL_CHARS] == 5000
        assert m[META_CONTEXT_REDUCTION] == "tool_result_pruned"


@pytest.mark.asyncio
async def test_budget_prefix_stability_across_calls():
    """Same input → same output (deterministic)."""
    messages = _make_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )
    result_1 = await gov.apply(messages, _CTX)
    result_2 = await gov.apply(messages, _CTX)

    assert result_1 == result_2


@pytest.mark.asyncio
async def test_budget_no_message_dropping():
    """Governance never drops messages — only replaces content."""
    messages = _make_messages(15, content_size=5000)
    gov = ContextBudgetGovernance(
        max_context_tokens=50_000,
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
        protect_tokens=20_000,
        min_gain_tokens=15_000,
        keep_recent=5,
    )
    result = await gov.apply(messages, _CTX)

    assert len(result) == len(messages)  # no messages dropped


@pytest.mark.asyncio
async def test_budget_uses_cached_token_count():
    """When token_count is cached on the message, governance uses it
    instead of re-estimating."""
    messages: list[dict[str, Any]] = [
        {"role": str(MessageRole.SYSTEM), "content": "sys", "token_count": 3},
        {"role": str(MessageRole.ASSISTANT), "content": "c0", "token_count": 2},
        {
            "role": str(MessageRole.TOOL),
            "name": "read_file",
            "content": "x" * 50000,  # huge content but cached token_count=1
            "tool_call_id": "c0",
            "token_count": 1,
        },
    ]
    gov = ContextBudgetGovernance(
        max_context_tokens=10,  # threshold = 6 → total cached = 3+2+1 = 6 ≤ 6 → no pruning
        token_estimator=_CharEstimator(),
        governance_ratio=0.60,
    )
    result = await gov.apply(messages, _CTX)
    assert result[2]["content"] == "x" * 50000


@pytest.mark.asyncio
async def test_all_strategies_return_copies():
    """所有策略都不应修改原始输入."""
    original = [
        {"role": str(MessageRole.ASSISTANT), "content": "hello"},
    ]

    for Gov, kwargs in {
        ToolChainRepairGovernance: {},
        ContextBudgetGovernance: {"max_context_tokens": 100},
    }.items():
        gov = Gov(**kwargs)
        result = await gov.apply(original, _CTX)
        assert result is not original
        assert original == [{"role": str(MessageRole.ASSISTANT), "content": "hello"}]


@pytest.mark.asyncio
async def test_budget_governance_uses_injected_estimator() -> None:
    """Custom estimator is used for token resolution."""
    class FixedEst(TokenEstimator):
        def estimate_text(self, text: str) -> int:
            return 5

    gov = ContextBudgetGovernance(
        max_context_tokens=100,
        token_estimator=FixedEst(),
    )
    msgs = [{"role": "user", "content": "x"}, {"role": "user", "content": "y"}]
    out = await gov.apply(msgs, _CTX)
    assert isinstance(out, list)
