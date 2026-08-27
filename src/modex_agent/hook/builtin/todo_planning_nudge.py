"""Todo planning nudge — remind a todo-owning agent to plan multi-step work.

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

Behavior-level backstop for the ``## Task Tracking`` system-prompt section:
when an agent that owns ``todo_write`` has a completely empty todo list and
has not used the todo tools in the recent history of the current turn, this
hook injects a one-shot reminder to plan with ``todo_write``.
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
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.store import TodoStore

_TODO_NUDGE_REMINDER = (
    "This session has no todo list yet. If the current task involves multiple "
    "steps, call `todo_write` now to plan and track them (see "
    '"## Task Tracking" in your system prompt). For single-step work, '
    "continue directly."
)


class TodoPlanningNudgeHook(BeforeTurnHook, BeforeIterationHook):
    """Deprecated one-shot per-turn reminder to plan with ``todo_write``.

    State machine (two hook points, stateless instance — Rule 1):

    - ``before_turn`` arms ``TODO_NUDGE_PENDING`` on every turn attempt
      (including continuations). Approval resume does not re-fire
      ``BEFORE_TURN``, so a resumed turn never re-evaluates.
    - ``before_iteration`` pops the flag on its first evaluation of the
      attempt, then:

      - gate failure (``todo_write`` unregistered, no pool-level todo
        store, or the session already has ANY todo item — pending,
        in-progress, or completed) — settled for this attempt, no
        injection;
      - ``USED`` verdict (a ``todo_write``/``todo_read`` call inside the
        current turn's recent-history window) — settled, no injection;
      - ``SHORT_TURN`` verdict (fewer than 3 assistant messages since the
        last user/agent message — includes the fresh-turn entry where the
        count is zero) — re-armed; later iterations of the same attempt
        re-evaluate once the turn accumulates steps;
      - ``DUE`` verdict (3 assistant steps without any todo tool call) —
        the reminder is appended once, then settled.

    The reminder is appended to ``ctx.history`` as a ``system_reminder``
    message — write-through cached and persisted — before the LLM request
    is built, so the current iteration already sees it.

    ``todo_store`` may be ``None`` on harnesses without a pool-level store
    (single-agent assemblies); the hook silently skips in that case. This
    hook is a behavioral nudge, not a continuation driver: it never reads
    or writes ``CONTINUATION_REQUEST`` / ``CONTINUATION_RENEW_MAX_TURNS``.
    """

    def __init__(self, todo_store: TodoStore | None) -> None:
        self._todo_store = todo_store

    @property
    def name(self) -> str:
        return "todo_planning_nudge"

    async def before_turn(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        state.custom[TurnCustomKey.TODO_NUDGE_PENDING] = True

    async def before_iteration(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        if not state.custom.pop(TurnCustomKey.TODO_NUDGE_PENDING, False):
            return

        tool_manager = ctx.tool_manager
        if tool_manager is None or not tool_manager.is_registered("todo_write"):
            return
        if self._todo_store is None:
            return

        todos = await self._todo_store.get(str(ctx.session))
        if todos:
            return

        messages = await ctx.history.to_list()
        verdict = scan_tool_usage_in_turn(
            messages, frozenset({"todo_write", "todo_read"})
        )
        if verdict is ToolNudgeVerdict.SHORT_TURN:
            state.custom[TurnCustomKey.TODO_NUDGE_PENDING] = True
            return
        if verdict is ToolNudgeVerdict.USED:
            return

        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(_TODO_NUDGE_REMINDER),
            }
        )
