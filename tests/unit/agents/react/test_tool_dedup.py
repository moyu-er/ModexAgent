"""Unit tests for ToolCallDeduplicator — same-step dedup and cross-step streak detection."""

from __future__ import annotations

from modex_agent.agents.react.tool_dedup import StreakAction, ToolCallDeduplicator
from modex_agent.core.tool_manager import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(content: str = "ok") -> ToolResult:
    return ToolResult(tool_name="test_tool", result=content, error=None)


def _simulate_consecutive_steps(
    dedup: ToolCallDeduplicator,
    tool_name: str,
    args: dict,
    count: int,
) -> StreakAction:
    """Simulate *count* consecutive steps that each call *tool_name* + *args*.

    Returns the StreakAction from the final step's ``check_streak`` call.
    """
    action = StreakAction(action="continue")
    for _ in range(count):
        dedup.begin_step()
        action = dedup.check_streak(tool_name, args)
        dedup.register_result(tool_name, args, _result())
        dedup.end_step()
    return action


# ---------------------------------------------------------------------------
# canonical_args / make_key
# ---------------------------------------------------------------------------


class TestCanonicalArgs:
    def test_order_invariant(self):
        a = ToolCallDeduplicator.canonical_args({"a": 1, "b": 2})
        b = ToolCallDeduplicator.canonical_args({"b": 2, "a": 1})
        assert a == b

    def test_ensure_ascii_false(self):
        result = ToolCallDeduplicator.canonical_args({"name": "中文"})
        assert "中文" in result

    def test_different_args_different_output(self):
        a = ToolCallDeduplicator.canonical_args({"a": 1})
        b = ToolCallDeduplicator.canonical_args({"a": 2})
        assert a != b


class TestMakeKey:
    def test_format(self):
        key = ToolCallDeduplicator.make_key("read_file", {"path": "/x"})
        assert key.startswith("read_file ")
        assert "/x" in key

    def test_order_invariant(self):
        k1 = ToolCallDeduplicator.make_key("tool", {"a": 1, "b": 2})
        k2 = ToolCallDeduplicator.make_key("tool", {"b": 2, "a": 1})
        assert k1 == k2

    def test_different_tools_different_keys(self):
        k1 = ToolCallDeduplicator.make_key("tool_a", {"x": 1})
        k2 = ToolCallDeduplicator.make_key("tool_b", {"x": 1})
        assert k1 != k2


# ---------------------------------------------------------------------------
# Same-step dedup
# ---------------------------------------------------------------------------


class TestSameStepDedup:
    def test_same_step_dedup(self):
        dedup = ToolCallDeduplicator()
        dedup.begin_step()

        result1 = _result("first")
        dedup.register_result("tool", {"a": 1}, result1)

        cached = dedup.check_same_step("tool", {"a": 1})
        assert cached is result1

    def test_same_step_no_dedup_different_args(self):
        dedup = ToolCallDeduplicator()
        dedup.begin_step()

        dedup.register_result("tool", {"a": 1}, _result("first"))

        cached = dedup.check_same_step("tool", {"a": 2})
        assert cached is None

    def test_same_step_no_dedup_different_tool(self):
        dedup = ToolCallDeduplicator()
        dedup.begin_step()

        dedup.register_result("tool_a", {"a": 1}, _result("first"))

        cached = dedup.check_same_step("tool_b", {"a": 1})
        assert cached is None

    def test_same_step_cleared_on_begin_step(self):
        dedup = ToolCallDeduplicator()
        dedup.begin_step()
        dedup.register_result("tool", {"a": 1}, _result("first"))

        dedup.begin_step()
        cached = dedup.check_same_step("tool", {"a": 1})
        assert cached is None


# ---------------------------------------------------------------------------
# Cross-step streak detection
# ---------------------------------------------------------------------------


class TestStreakDetection:
    def test_streak_continue_first_occurrence(self):
        """First-ever call — no streak, should return CONTINUE."""
        dedup = ToolCallDeduplicator()
        dedup.begin_step()
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "continue"
        assert action.streak == 0

    def test_streak_continue_second_step(self):
        """Second consecutive step — streak=1, still CONTINUE (< 3)."""
        dedup = ToolCallDeduplicator()
        action = _simulate_consecutive_steps(dedup, "tool", {"a": 1}, count=1)

        dedup.begin_step()
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "continue"
        assert action.streak == 1

    def test_streak_remind_at_3(self):
        """After 3 consecutive steps, should return REMIND."""
        dedup = ToolCallDeduplicator()
        # Simulate 3 full steps to build up streak count
        _simulate_consecutive_steps(dedup, "tool", {"a": 1}, count=3)

        # Fourth step: check_streak before registering
        dedup.begin_step()
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "remind"
        assert action.streak == 3
        assert "3 times consecutively" in action.reminder

    def test_streak_remind_at_5(self):
        """After 5 consecutive steps, should return REMIND with tier-2 message."""
        dedup = ToolCallDeduplicator()
        _simulate_consecutive_steps(dedup, "tool", {"a": 1}, count=5)

        dedup.begin_step()
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "remind"
        assert action.streak == 5
        assert "likely unproductive" in action.reminder

    def test_streak_skip_at_8(self):
        """After 8 consecutive steps, should return SKIP."""
        dedup = ToolCallDeduplicator()
        _simulate_consecutive_steps(dedup, "tool", {"a": 1}, count=8)

        dedup.begin_step()
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "skip"
        assert action.streak == 8
        assert "Stop calling this tool" in action.reminder

    def test_streak_stop_at_12(self):
        """After 12 consecutive steps, should return STOP."""
        dedup = ToolCallDeduplicator()
        _simulate_consecutive_steps(dedup, "tool", {"a": 1}, count=12)

        dedup.begin_step()
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "stop"
        assert action.streak == 12
        assert "turn is being terminated" in action.reminder

    def test_streak_reset_on_different_call(self):
        """Streak resets to 0 when a different call appears between repetitions."""
        dedup = ToolCallDeduplicator()

        # Step 1: call tool A
        dedup.begin_step()
        dedup.check_streak("tool_a", {"x": 1})
        dedup.register_result("tool_a", {"x": 1}, _result())
        dedup.end_step()

        # Step 2: call tool B (different) — resets prev_step_keys
        dedup.begin_step()
        dedup.check_streak("tool_b", {"x": 1})
        dedup.register_result("tool_b", {"x": 1}, _result())
        dedup.end_step()

        # Step 3: call tool A again — should be CONTINUE (streak=0, not in prev_step)
        dedup.begin_step()
        action = dedup.check_streak("tool_a", {"x": 1})
        assert action.action == "continue"
        assert action.streak == 0

    def test_streak_reset_on_interleaved(self):
        """Streak resets when a different call interleaves."""
        dedup = ToolCallDeduplicator()

        # Step 1: call tool A
        dedup.begin_step()
        dedup.register_result("tool_a", {"x": 1}, _result())
        dedup.end_step()

        # Step 2: call tool A + tool B (interleaved)
        dedup.begin_step()
        action_a = dedup.check_streak("tool_a", {"x": 1})
        assert action_a.streak == 1  # still in prev_step_keys
        dedup.register_result("tool_a", {"x": 1}, _result())
        dedup.register_result("tool_b", {"x": 2}, _result())
        dedup.end_step()

        # Step 3: call only tool B — tool A streak should reset
        dedup.begin_step()
        action_b = dedup.check_streak("tool_b", {"x": 2})
        assert action_b.streak == 1  # B was in prev step
        dedup.register_result("tool_b", {"x": 2}, _result())
        dedup.end_step()

        # Step 4: call tool A — streak should be 0 (not in prev_step_keys)
        dedup.begin_step()
        action_a2 = dedup.check_streak("tool_a", {"x": 1})
        assert action_a2.streak == 0
        assert action_a2.action == "continue"


# ---------------------------------------------------------------------------
# Integration-style: full step lifecycle
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_same_step_dedul_within_streak(self):
        """Same-step dedup and streak detection work together."""
        dedup = ToolCallDeduplicator()

        # Build a streak of 3 steps
        _simulate_consecutive_steps(dedup, "tool", {"a": 1}, count=3)

        # Step 4: two identical calls in same step
        dedup.begin_step()

        # First call — streak=3, REMIND
        action = dedup.check_streak("tool", {"a": 1})
        assert action.action == "remind"
        r1 = _result("first")
        dedup.register_result("tool", {"a": 1}, r1)

        # Second call — same step dedup returns cached
        cached = dedup.check_same_step("tool", {"a": 1})
        assert cached is r1

        dedup.end_step()

    def test_progression_through_all_tiers(self):
        """Verify streak progresses through all action tiers."""
        dedup = ToolCallDeduplicator()
        tool_name = "my_tool"
        args = {"path": "/file"}

        actions: list[str] = []
        for _step in range(15):
            dedup.begin_step()
            action = dedup.check_streak(tool_name, args)
            actions.append(action.action)
            dedup.register_result(tool_name, args, _result())
            dedup.end_step()

        # Step 0 (streak=0): continue
        # Step 1 (streak=1): continue
        # Step 2 (streak=2): continue
        # Step 3 (streak=3): remind
        # Step 4 (streak=4): remind
        # Step 5 (streak=5): remind
        # Step 6 (streak=6): remind
        # Step 7 (streak=7): remind
        # Step 8 (streak=8): skip
        # Step 9 (streak=9): skip
        # Step 10 (streak=10): skip
        # Step 11 (streak=11): skip
        # Step 12 (streak=12): stop
        # Step 13 (streak=13): stop
        # Step 14 (streak=14): stop
        assert actions[0] == "continue"
        assert actions[2] == "continue"
        assert actions[3] == "remind"
        assert actions[7] == "remind"
        assert actions[8] == "skip"
        assert actions[11] == "skip"
        assert actions[12] == "stop"
        assert actions[14] == "stop"
