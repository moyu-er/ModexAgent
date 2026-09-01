"""Cross-step tool-call streak detection for the ReAct ToolNode.

Same-step duplicate pruning now runs in ToolNode's scheduler so followers can
participate in ordered completion and cancellation. The old ``check_same_step``
entry point has been removed. This module tracks repeated ``(tool_name, args)``
pairs across consecutive ReAct iterations, escalating ``<system-reminder>``
messages before eventually skipping or cancelling.

The deduplicator is **per-turn**: create a fresh instance at the start
of each ``ReActAgent.run()`` and pass it to ``build_react_graph()``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.tool_manager import ToolResult

__all__ = ["StreakAction", "StreakDecision", "ToolCallDeduplicator"]


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class StreakDecision(StrEnum):
    """Action prescribed by :meth:`ToolCallDeduplicator.check_streak`."""

    CONTINUE = "continue"
    REMIND = "remind"
    SKIP = "skip"
    STOP = "stop"


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


class StreakAction(BaseModel):
    """Decision returned by :meth:`ToolCallDeduplicator.check_streak`.

    Attributes:
        action: The :class:`StreakDecision` indicating how to proceed.
        reminder: The reminder text to surface to the LLM (empty when
            *action* is ``CONTINUE``).
        streak: The current consecutive-step count for this call key.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: StreakDecision
    reminder: str = ""
    streak: int = 0


# ---------------------------------------------------------------------------
# Streak thresholds
# ---------------------------------------------------------------------------

_STREAK_REMIND = 3  # >= 3 and < 5
_STREAK_REMIND_2 = 5  # >= 5 and < 8
_STREAK_SKIP = 8  # >= 8 and < 12
_STREAK_STOP = 12  # >= 12


def _reminder_tier1(tool_name: str, streak: int) -> str:
    return (
        f"<system-reminder>You have called {tool_name} with the same arguments "
        f"{streak} times consecutively. State what new information you expect "
        f"this call to produce, or try a different approach.</system-reminder>"
    )


def _reminder_tier2(tool_name: str, streak: int) -> str:
    return (
        f"<system-reminder>You have called {tool_name} with the same arguments "
        f"{streak} times. This is likely unproductive. Choose one: "
        f"(1) verify your assumption is correct and the tool is working, "
        f"(2) try different arguments or a different tool, "
        f"(3) conclude this line of investigation and write your response."
        f"</system-reminder>"
    )


def _reminder_tier3(tool_name: str, streak: int) -> str:
    return (
        f"<system-reminder>You have called {tool_name} with the same arguments "
        f"{streak} times. Stop calling this tool. Write your final response "
        f"now based on what you already know.</system-reminder>"
    )


def _reminder_tier4(tool_name: str, streak: int) -> str:
    return (
        f"<system-reminder>You have called {tool_name} with the same arguments "
        f"{streak} times. The turn is being terminated to prevent further "
        f"unproductive repetition. Write your final response based on what "
        f"you already know.</system-reminder>"
    )


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------


class ToolCallDeduplicator:
    """Tracks tool calls within and across ReAct steps for deduplication.

    Lifecycle (per turn):

    * ``begin_step()`` — called at the start of each ``ToolNode.execute``.
    * For each tool call in the batch:
        - ``check_streak()`` — decide whether to execute / remind / skip / stop.
        - ``register_result()`` — record the leader key for streak tracking.
    * ``end_step()`` — update cross-step streak counts after the batch.
    """

    # Keys seen in the current step
    _step_keys: set[str]
    # Keys seen in the previous step
    _prev_step_keys: set[str]
    # Per-key consecutive-step streak count
    _streak_counts: dict[str, int]

    def __init__(self) -> None:
        self._step_keys: set[str] = set()
        self._prev_step_keys: set[str] = set()
        self._streak_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Key computation
    # ------------------------------------------------------------------

    @staticmethod
    def canonical_args(args: dict[str, Any]) -> str:
        """Return a canonical, order-independent JSON string for *args*."""
        return json.dumps(args, sort_keys=True, ensure_ascii=False)

    @classmethod
    def make_key(cls, tool_name: str, args: dict[str, Any]) -> str:
        """Return a deduplication key from *tool_name* and *args*."""
        return f"{tool_name} {cls.canonical_args(args)}"

    # ------------------------------------------------------------------
    # Per-step lifecycle
    # ------------------------------------------------------------------

    def begin_step(self) -> None:
        """Clear per-step state at the start of a ToolNode execution.

        Moves the current step's keys to ``_prev_step_keys`` so that
        :meth:`check_streak` can detect cross-step repetition.
        """
        self._prev_step_keys = self._step_keys
        self._step_keys = set()

    def end_step(self) -> None:
        """Update cross-step streak counts after the batch completes.

        For each key in the current step:
        - If it was also in the previous step, increment its streak.
        - Otherwise, reset its streak to 0.

        Keys that no longer appear are removed from ``_streak_counts``.
        """
        new_counts: dict[str, int] = {}
        for key in self._step_keys:
            if key in self._prev_step_keys:
                new_counts[key] = self._streak_counts.get(key, 0) + 1
            else:
                new_counts[key] = 0
        self._streak_counts = new_counts

    # ------------------------------------------------------------------
    def register_result(self, tool_name: str, args: dict[str, Any], result: ToolResult) -> None:
        """Record a completed leader call key for cross-step streak tracking."""
        key = self.make_key(tool_name, args)
        self._step_keys.add(key)

    # ------------------------------------------------------------------
    # Cross-step streak detection
    # ------------------------------------------------------------------

    def check_streak(self, tool_name: str, args: dict[str, Any]) -> StreakAction:
        """Return the streak action for *tool_name* + *args*.

        Called *before* executing a tool call. The streak count is derived
        from whether this key appeared in the previous step and the running
        ``_streak_counts`` value.

        Returns:
            A :class:`StreakAction` indicating how the caller should proceed.
        """
        key = self.make_key(tool_name, args)
        streak = self._streak_counts.get(key, 0) + 1 if key in self._prev_step_keys else 0

        if streak >= _STREAK_STOP:
            return StreakAction(
                action=StreakDecision.STOP,
                reminder=_reminder_tier4(tool_name, streak),
                streak=streak,
            )
        if streak >= _STREAK_SKIP:
            return StreakAction(
                action=StreakDecision.SKIP,
                reminder=_reminder_tier3(tool_name, streak),
                streak=streak,
            )
        if streak >= _STREAK_REMIND_2:
            return StreakAction(
                action=StreakDecision.REMIND,
                reminder=_reminder_tier2(tool_name, streak),
                streak=streak,
            )
        if streak >= _STREAK_REMIND:
            return StreakAction(
                action=StreakDecision.REMIND,
                reminder=_reminder_tier1(tool_name, streak),
                streak=streak,
            )
        return StreakAction(action=StreakDecision.CONTINUE, streak=streak)
