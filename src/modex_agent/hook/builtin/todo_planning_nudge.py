"""Todo planning nudge — one-shot reminder to plan multi-step work.

Behavior-level backstop for the ``todo`` capability's ``todo.discipline``
prompt section (``## Task Discipline``): when an agent that owns
``todo_write`` has a completely empty todo list and has accumulated
three assistant steps in the current logical turn without touching the
todo tools, inject a one-shot ``system-reminder`` nudging the model to
plan with ``todo_write``.

Arming point — ``start_node_turn``, the fresh-turn dispatch that fires
exactly once per logical turn. (The retired implementation armed on
``before_turn`` — per turn-ATTEMPT — so continuation attempts re-armed
the flag while the backward scan's window spans the whole logical turn,
double-nudging within one turn and mis-suppressing across turns.
Fresh-turn arming fixes the attempt/window mismatch structurally:
continuation re-entry never passes StartNode, and approval resume
routes START→TOOL without re-dispatch.)

State machine (two hook points, stateless instance — hook Rule 1):

- ``start_node_turn`` arms ``custom[TurnCustomKey.TODO_NUDGE_PENDING]``
  on fresh turns only.
- ``before_iteration`` pops the flag on first evaluation, then:

  - gate failure (``todo_write`` unregistered, no todo store, or the
    session already has ANY todo item — pending, in-progress, or
    completed) — settled for this turn, no injection;
  - ``USED`` verdict (a ``todo_write``/``todo_read`` call inside the
    current turn's recent-history window) — settled, no injection;
  - ``SHORT_TURN`` verdict (fewer than 3 assistant messages since the
    last user/agent message) — re-armed; later iterations of the same
    turn re-evaluate once the turn accumulates steps;
  - ``DUE`` verdict (3 assistant steps without any todo tool call) —
    the reminder is appended once, then settled.

The reminder lands in ``ctx.history`` as a ``system_reminder`` message
(LLM-visible only) before the LLM request is built, so the current
iteration already sees it. ``scan_tool_usage_in_turn`` treats
system-reminders as transparent, so co-resident injectors (loop
detection, length guard, todo continuation) neither fragment this
hook's scan nor vice versa.

``todo_store`` may be ``None`` on harnesses without a pool-level store
(single-agent assemblies); the hook silently skips in that case. The
``todo_write`` registration gate covers roster-referenced-but-toolless
assemblies (a pool-mate's capability makes the supply exist while this
agent's roster never carried the tools). This hook is a behavioral
nudge, not a continuation driver: it never reads or writes
``CONTINUATION_REQUEST`` / ``CONTINUATION_RENEW_MAX_TURNS``.
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
from modex_agent.hook.abc import BeforeIterationHook, StartNodeTurnHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.store import TodoStore

_TODO_NUDGE_REMINDER = (
    "You have completed several steps in this turn without using todo "
    "tools. If the task ahead still involves multiple steps, consider "
    "planning them with `todo_write` to keep progress visible and "
    "recoverable. If the work is nearly done or simple, just continue."
)


class TodoPlanningNudgeHook(StartNodeTurnHook, BeforeIterationHook):
    """One-shot per-logical-turn reminder to plan with ``todo_write``."""

    def __init__(self, todo_store: TodoStore | None) -> None:
        self._todo_store = todo_store

    @property
    def name(self) -> str:
        return "todo_planning_nudge"

    async def start_node_turn(self, ctx: AgentContext) -> None:
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
