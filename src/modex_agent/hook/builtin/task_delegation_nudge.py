"""Task delegation nudge — remind a subagent-owning agent to dispatch.

.. deprecated::
    DEPRECATED — effectiveness was poor in practice: evaluated at
    turn-entry moments where the current turn had produced no assistant
    steps yet, the old scan read the *previous* turn's tail (fresh-turn
    double-nudge, cross-turn suppression), and the reminder fired before
    the model had any in-turn context to act on. The shipped declarations
    no longer roster-reference this hook. If revived: arm the pending
    flag from ``start_node_turn`` (fresh-turn-only — continuation attempts
    never re-enter) instead of ``before_turn`` (per-attempt re-arm), and
    keep the ``scan_tool_usage_in_turn`` verdict machine below.

Behavior-level backstop for the delegation guidance: the system prompt
carries the static when/how policy, the ``task`` tool description carries
the live roster, and this hook fires when an agent that owns idle
subagents has not used ``task`` in the recent history of the current turn.
"""

from __future__ import annotations

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.message_utils import (
    ToolNudgeVerdict,
    scan_tool_usage_in_turn,
    wrap_system_reminder,
)
from modex_agent.core.types import MessageRole
from modex_agent.hook.abc import BeforeIterationHook, BeforeTurnHook
from modex_agent.multi_agent.tools import TaskDispatchTool
from modex_agent.runtime.enums import TurnCustomKey

_TASK_NUDGE_REMINDER = (
    "You have not dispatched any work to your subagents in the recent history "
    "of this turn. If parts of the current task fit a subagent — bulk "
    "investigation, an independent implementation piece, or verification of a "
    "deliverable — dispatch them with the `task` tool (brief format: "
    '"Delegating To Subagents" in your system prompt). If the work is a '
    "needle query or trivial, continue directly."
)


class TaskDelegationNudgeHook(BeforeTurnHook, BeforeIterationHook):
    """Deprecated one-shot per-turn reminder to use the ``task`` tool.

    State machine (two hook points, stateless instance — Rule 1):

    - ``before_turn`` arms ``TASK_NUDGE_PENDING`` on every turn attempt
      (including continuations). Approval resume does not re-fire
      ``BEFORE_TURN``, so a resumed turn never re-evaluates.
    - ``before_iteration`` pops the flag on its first evaluation of the
      attempt, then:

      - gate failure (``task`` unregistered, or the dispatch tool's live
        roster is empty) — settled for this attempt, no injection;
      - ``USED`` verdict (a ``task`` call inside the current turn's
        recent-history window) — settled, no injection;
      - ``SHORT_TURN`` verdict (fewer than 3 assistant messages since the
        last user/agent message — includes the fresh-turn entry where the
        count is zero) — re-armed; later iterations of the same attempt
        re-evaluate once the turn accumulates steps;
      - ``DUE`` verdict (3 assistant steps without any ``task`` call) —
        the reminder is appended once, then settled.

    The reminder is appended to ``ctx.history`` as a ``system_reminder``
    message — write-through cached and persisted — before the LLM request
    is built, so the current iteration already sees it.

    This hook is a behavioral nudge, not a continuation driver: it never
    reads or writes ``CONTINUATION_REQUEST`` / ``CONTINUATION_RENEW_MAX_TURNS``.
    """

    @property
    def name(self) -> str:
        return "task_delegation_nudge"

    async def before_turn(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        state.custom[TurnCustomKey.TASK_NUDGE_PENDING] = True

    async def before_iteration(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        if not state.custom.pop(TurnCustomKey.TASK_NUDGE_PENDING, False):
            return

        tool_manager = ctx.tool_manager
        if tool_manager is None or not tool_manager.is_registered("task"):
            return
        # Tool-slot narrowing: "task" resolves to TaskDispatchTool by the
        # framework's own factory; the concrete type carries the roster API.
        tool = tool_manager.get_tool("task")
        if not isinstance(tool, TaskDispatchTool) or not tool.list_targets():
            return

        messages = await ctx.history.to_list()
        verdict = scan_tool_usage_in_turn(messages, frozenset({"task"}))
        if verdict is ToolNudgeVerdict.SHORT_TURN:
            state.custom[TurnCustomKey.TASK_NUDGE_PENDING] = True
            return
        if verdict is ToolNudgeVerdict.USED:
            return

        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(_TASK_NUDGE_REMINDER),
            }
        )
