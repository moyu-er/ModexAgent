"""Tests for tool-chain aware compression."""


from framework.memory.compression.tool_chain import (
    _find_safe_truncation_count,
    _fit_token_window,
)


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
