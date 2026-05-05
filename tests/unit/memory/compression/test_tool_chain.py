"""Tests for tool-chain aware compression and boundary policies."""

from framework.memory.compaction.boundary import (
    ToolChainBoundaryPolicy,
    UserTurnToolChainBoundaryPolicy,
)
from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    MessageCompactionDecision,
)
from framework.memory.compression.tool_chain import (
    _find_safe_truncation_count,
    _fit_token_window,
)
from framework.memory.core.scope import MemoryContext


class TestFindSafeTruncationCount:
    def test_basic_excess(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(5)]
        # excess=2, protected=0, tail_keep=1 => boundary=2 (keep last 1)
        assert _find_safe_truncation_count(msgs, 2) == 2

    def test_respects_protected_count(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(5)]
        # excess=3, protected=1, tail_keep=1 => boundary=4 (remove indices 1,2,3)
        assert _find_safe_truncation_count(msgs, 3, protected_count=1, min_tail_keep=1) == 4

    def test_clamps_to_deletable_region(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(5)]
        # excess=10, protected=1, tail_keep=1 => max_boundary=4
        assert _find_safe_truncation_count(msgs, 10, protected_count=1, min_tail_keep=1) == 4

    def test_graceful_degradation_when_budget_impossible(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(3)]
        # protected=2, tail_keep=2 => 2+2 >= 3, return protected_count
        assert _find_safe_truncation_count(msgs, 5, protected_count=2, min_tail_keep=2) == 2

    def test_does_not_split_tool_chain(self):
        msgs = [
            {"role": "user", "content": "0"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "r1", "tool_call_id": "tc1"},
            {"role": "user", "content": "3"},
            {"role": "user", "content": "4"},
        ]
        # boundary falls inside chain at index 2 -> extend to chain_end+1=3
        assert _find_safe_truncation_count(msgs, 2) == 3

    def test_protected_summary_survives_token_pressure(self):
        """Protected summary at head should not be removed."""
        msgs = [
            {"role": "system", "content": "[Earlier conversation compressed] summary"},
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
            {"role": "user", "content": "3"},
        ]
        boundary = _find_safe_truncation_count(msgs, 2, protected_count=1, min_tail_keep=1)
        assert boundary >= 1
        pruned = msgs[1:boundary]
        assert msgs[0] not in pruned

    def test_latest_user_turn_is_preserved(self):
        """min_tail_keep should preserve the latest user turn."""
        msgs = [
            {"role": "user", "content": "0"},
            {"role": "assistant", "content": "1"},
            {"role": "user", "content": "2"},
        ]
        boundary = _find_safe_truncation_count(msgs, 2, protected_count=0, min_tail_keep=1)
        assert boundary <= 2


class TestFitTokenWindow:
    def test_no_truncation_when_under_budget(self):
        msgs = [{"role": "user", "content": "hello"}]
        remaining, pruned = _fit_token_window(msgs, 1000)
        assert len(remaining) == 1
        assert pruned == []

    def test_protected_head_not_removed(self):
        msgs = [
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "a" * 100},
            {"role": "user", "content": "b" * 100},
            {"role": "user", "content": "c" * 100},
        ]
        remaining, pruned = _fit_token_window(msgs, 30, protected_count=1, min_tail_keep=1)
        assert remaining[0]["role"] == "system"
        assert remaining[0]["content"] == "summary"

    def test_graceful_degradation_when_protected_plus_tail_exceeds_budget(self):
        msgs = [
            {"role": "system", "content": "summary"},
            {"role": "user", "content": "a" * 100},
            {"role": "user", "content": "b" * 100},
        ]
        # protected=1, tail_keep=2 => 1+2 >= 3, should degrade
        remaining, pruned = _fit_token_window(msgs, 10, protected_count=1, min_tail_keep=2)
        assert len(pruned) == 0
        assert len(remaining) == 3

    def test_keeps_tool_chain_intact(self):
        msgs = [
            {"role": "user", "content": "0"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "tc1"},
            {"role": "user", "content": "a" * 100},
            {"role": "user", "content": "b" * 100},
        ]
        remaining, pruned = _fit_token_window(msgs, 30)
        valid_tool_call_ids = {
            tc.get("id")
            for m in remaining
            if m.get("role") == "assistant" and m.get("tool_calls")
            for tc in m["tool_calls"]
        }
        for m in remaining:
            if m.get("role") == "tool":
                assert m.get("tool_call_id") in valid_tool_call_ids

    def test_min_tail_keep_honored(self):
        msgs = [
            {"role": "user", "content": "a" * 100},
            {"role": "user", "content": "b" * 100},
            {"role": "user", "content": "c" * 100},
            {"role": "user", "content": "d" * 100},
        ]
        remaining, pruned = _fit_token_window(msgs, 30, protected_count=0, min_tail_keep=2)
        assert len(remaining) >= 2


# ── Boundary policy regression tests ────────────────────────────────────────

_CTX = MemoryContext(session_id="test", user_id="u1")


class TestToolChainBoundaryPolicy:
    """Regression tests for ToolChainBoundaryPolicy with real decisions."""

    def test_boundary_avoids_splitting_tool_chain(self):
        """Boundary should not split an assistant tool_calls + tool result pair."""
        policy = ToolChainBoundaryPolicy()

        msgs = [
            {"role": "user", "content": "0"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "tc1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "tc1"},
            {"role": "user", "content": "3"},
            {"role": "assistant", "content": "4"},
        ]
        # Conservative defaults: user+assistant=SUMMARIZE, tool=DROP_FROM_SUMMARY,
        # assistant with tool_calls=KEEP_RAW
        cp = ConservativeCompactionPolicy()
        decisions = cp.decide_all(msgs, _CTX, "token_pressure")

        # target_prune=2: boundary 1 means prune idx 0 only, chain at 1-2 intact
        boundary = policy.find_prune_boundary(msgs, decisions, 2)
        # chain (assistant tool_calls + tool result) must not be split
        chain_start = 1
        chain_end = 2
        assert not (chain_start < boundary <= chain_end), \
            f"boundary={boundary} should not split tool chain [{chain_start},{chain_end}]"

    def test_keep_raw_shrinks_boundary(self):
        """When KEEP_RAW appears in prune range, boundary shrinks before it."""
        policy = ToolChainBoundaryPolicy()

        msgs = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
            {"role": "user", "content": "3"},
        ]
        # Mark system message manually as KEEP_RAW
        decisions = [
            MessageCompactionDecision.KEEP_RAW,
            MessageCompactionDecision.SUMMARIZE,
            MessageCompactionDecision.SUMMARIZE,
            MessageCompactionDecision.SUMMARIZE,
        ]

        boundary = policy.find_prune_boundary(msgs, decisions, 2)
        # boundary must be <= 0 because index 0 is KEEP_RAW
        assert boundary == 0

    def test_empty_decisions_no_effect(self):
        """Empty decisions list is handled (no KEEP_RAW protection)."""
        policy = ToolChainBoundaryPolicy()
        msgs = [
            {"role": "user", "content": "1"},
            {"role": "user", "content": "2"},
            {"role": "user", "content": "3"},
        ]
        boundary = policy.find_prune_boundary(msgs, [], 1)
        assert boundary == 1  # simple prune without decisions

    def test_with_conservative_defaults(self):
        """Integration: ConservativeCompactionPolicy + ToolChainBoundaryPolicy."""
        policy = ToolChainBoundaryPolicy()
        cp = ConservativeCompactionPolicy()

        msgs = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "read_file"}}
            ]},
            {"role": "tool", "tool_call_id": "t1", "name": "read_file", "content": "file content"},
            {"role": "assistant", "content": "file says hello"},
            {"role": "user", "content": "thanks"},
            {"role": "assistant", "content": "you're welcome"},
        ]
        decisions = cp.decide_all(msgs, _CTX, "token_pressure")

        # target_prune=3; tool chain at (1,2) must not be split
        boundary = policy.find_prune_boundary(msgs, decisions, 3)
        chain_start, chain_end = 1, 2
        assert not (chain_start < boundary <= chain_end), \
            f"boundary={boundary} splits tool chain [{chain_start},{chain_end}]"

        # suffix must contain the final messages
        keep = msgs[boundary:]
        assert keep[-2]["content"] == "thanks"
        assert keep[-1]["content"] == "you're welcome"


class TestUserTurnBoundaryPolicy:
    """Regression tests for UserTurnToolChainBoundaryPolicy."""

    def test_prefers_user_message_boundary(self):
        """Should prefer cutting before a user message."""
        policy = UserTurnToolChainBoundaryPolicy()

        msgs = [
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "answer 1"},
            {"role": "user", "content": "question 2"},
            {"role": "assistant", "content": "answer 2"},
        ]
        cp = ConservativeCompactionPolicy()
        decisions = cp.decide_all(msgs, _CTX, "token_pressure")

        boundary = policy.find_prune_boundary(msgs, decisions, 2)
        # Should prefer index 1 (user "question 1") or index 3 (user "question 2")
        # The base boundary from parent would be 2, but user-turn prefers 1 or 3
        assert msgs[boundary]["role"] == "user", f"expected user at boundary, got {msgs[boundary]['role']}"

    def test_prefers_completed_assistant_boundary(self):
        """Should prefer cutting after a completed assistant (no tool_calls)."""
        policy = UserTurnToolChainBoundaryPolicy()

        msgs = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        cp = ConservativeCompactionPolicy()
        decisions = cp.decide_all(msgs, _CTX, "token_pressure")

        boundary = policy.find_prune_boundary(msgs, decisions, 1)
        # base boundary=1 (index 1), lookahead: index 1 is "a1" (assistant without tool_calls)
        # so return idx+1=2 (after the completed assistant)
        assert msgs[boundary]["role"] == "user", f"expected user at boundary, got role={msgs[boundary]['role']}"

    def test_falls_back_to_parent_when_no_good_boundary(self):
        """When no user-turn boundary is found, falls back to parent."""
        policy = UserTurnToolChainBoundaryPolicy(lookahead=1)

        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "t1"},
            {"role": "assistant", "content": "reply"},
        ]
        cp = ConservativeCompactionPolicy()
        decisions = cp.decide_all(msgs, _CTX, "token_pressure")

        boundary = policy.find_prune_boundary(msgs, decisions, 1)
        # parent would have moved boundary to 0 to protect tool chain
        assert boundary == 0

    def test_never_moves_past_parent_safe_boundary(self):
        """User-turn boundary must not extend past parent safe boundary."""
        parent = ToolChainBoundaryPolicy()
        policy = UserTurnToolChainBoundaryPolicy(lookahead=5)

        msgs = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "result", "tool_call_id": "t1"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ]
        decisions = ConservativeCompactionPolicy().decide_all(msgs, _CTX, "token_pressure")

        parent_boundary = parent.find_prune_boundary(msgs, decisions, 3)
        boundary = policy.find_prune_boundary(msgs, decisions, 3)

        # User-turn policy must not extend past the parent's tool-chain-safe boundary
        assert boundary <= parent_boundary, \
            f"user-turn boundary {boundary} extends past parent {parent_boundary}"
