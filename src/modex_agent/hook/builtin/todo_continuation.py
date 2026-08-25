"""Todo-driven continuation with progress-sensitive deadlock prevention.

Registered first among AfterTurnHook continuation sources so its reminder
(including the active todo list) lands before other hooks' reminders.  It is
one of the two hooks that set ``CONTINUATION_RENEW_MAX_TURNS`` (the other is
``LengthGuardHook``) — the watchdog signal that authorizes the gate to
extend ``MAX_TURNS`` past the current upper bound while the agent is still
making progress (todos here, degenerate-ending recovery there).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.store import TodoItem
from modex_agent.tools.standard.todo_tool import TodoReadTool

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager


def _active_todo_hash(active: list[TodoItem]) -> str:
    payload = "|".join(sorted(f"{todo.content}:{todo.status.value}" for todo in active))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class TodoContinuationHook(AfterTurnHook):
    """Request continuation when active todo tasks remain after a turn attempt.

    Each hook acts independently — no OR/AND coordination with other
    AfterTurnHook continuation sources.  This hook:
      1. Reads active (pending + in_progress) todos.
      2. If none — clears the cached signature and returns.
      3. If the active-todo signature is unchanged since the last check —
         returns (deadlock: no progress made).
      4. Otherwise — injects a ``<system-reminder>`` with the full active
          todo list, sets ``CONTINUATION_REQUEST``, and sets
         ``CONTINUATION_RENEW_MAX_TURNS`` (watchdog: authorizes the gate to
         extend MAX_TURNS by 1 when the agent is still making progress).
    """

    def __init__(self, tree: SessionTreeManager | None = None) -> None:
        self._tree = tree

    @property
    def name(self) -> str:
        return "todo_continuation"

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason in (StopReason.TURN_CANCELLED, StopReason.ERROR):
            return

        tool_manager = ctx.tool_manager
        if tool_manager is None:
            return
        read_tool = tool_manager.get_tool("todo_read")
        if not isinstance(read_tool, TodoReadTool):
            return
        # Tech debt: private attr. Future: add todo_store to AgentRuntimeServices.
        todo_store = read_tool._store

        react_state = get_react_state(ctx)
        if react_state is None:
            return

        if self._tree is not None:
            tree_id = await self._tree.tree_id_for_session(str(ctx.session))
            if tree_id is not None:
                active_subtree = await self._tree.get_active_subtree_nodes(
                    tree_id, str(ctx.session)
                )
                if len(active_subtree) > 1:
                    return

        todos = await todo_store.get(str(ctx.session))
        active = [
            todo
            for todo in todos
            if todo.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
        ]

        if not active:
            react_state.custom.pop(TurnCustomKey.LAST_CONTINUATION_TODO_SIG, None)
            return

        current_signature = _active_todo_hash(active)
        cached_signature = react_state.custom.get(
            TurnCustomKey.LAST_CONTINUATION_TODO_SIG
        )
        if cached_signature is not None and cached_signature == current_signature:
            return

        todo_lines: list[str] = []
        for todo in active:
            marker = (
                "🔄"
                if todo.status == TodoStatus.IN_PROGRESS
                else "⬜"
            )
            todo_lines.append(f"{marker} {todo.content}")

        reminder = (
            f"You have {len(active)} active todo task(s) remaining:\n\n"
            + "\n".join(todo_lines)
            + "\n\n"
            "You should:\n"
            "- **Continue**: work on the next pending/in_progress todo item\n"
            "- **Cancel**: if the remaining tasks are no longer needed, mark "
            "them as cancelled with todo_write\n"
            "- **Acknowledge stuck**: if a task genuinely cannot be completed, "
            "leave its status unchanged and explain the blocker in your "
            "response — do NOT mark it completed unless it is actually done"
        )
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(reminder),
            }
        )

        react_state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] = (
            current_signature
        )
        react_state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        react_state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] = True
