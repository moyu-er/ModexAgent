"""Task delegation nudge — remind a subagent-owning agent to dispatch.

Behavior-level backstop for the delegation guidance: the system prompt
carries the static when/how policy, the ``task`` tool description carries
the live roster, and this hook fires when an agent that owns idle
subagents has not used ``task`` in the recent history of the current turn.
"""

from __future__ import annotations

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.message_utils import recent_tool_usage, wrap_system_reminder
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
    """One-shot per-turn reminder to use the ``task`` tool when idle.

    State machine (two hook points, stateless instance — Rule 1):

    - ``before_turn`` sets ``TASK_NUDGE_PENDING`` unconditionally on every
      turn attempt (including continuations). Approval resume does not
      re-fire ``BEFORE_TURN``, so a resumed turn never re-evaluates.
    - ``before_iteration`` pops the flag on its first — and only —
      evaluation of the attempt. Popping regardless of the outcome prevents
      repeated injection within one attempt; a fresh attempt re-arms the
      flag, and the recent-usage scan suppresses it when the agent has
      since dispatched.

    Gates (all must hold to inject): the ``task`` tool is registered, the
    dispatch tool reports at least one available subagent target, and no
    ``task`` call appears within the recent-history window. The reminder is
    appended to ``ctx.history`` as a ``system_reminder`` message —
    write-through cached and persisted — before the LLM request is built,
    so the current iteration already sees it.

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
        if recent_tool_usage(messages, frozenset({"task"})):
            return

        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(_TASK_NUDGE_REMINDER),
            }
        )
