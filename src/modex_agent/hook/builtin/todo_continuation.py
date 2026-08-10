"""Todo-driven continuation with progress-sensitive deadlock prevention."""

from __future__ import annotations

import hashlib

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


def _active_todo_hash(active: list[TodoItem]) -> str:
    payload = "|".join(sorted(f"{todo.content}:{todo.status.value}" for todo in active))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class TodoContinuationHook(AfterTurnHook):
    """Request ReAct continuation when active todo tasks remain after an attempt."""

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

        max_turns = react_state.custom.get(TurnCustomKey.MAX_TURNS, 1)
        if react_state.turn_attempt >= max_turns:
            return
        if TurnCustomKey.CONTINUATION_REQUEST in react_state.custom:
            return

        todos = await todo_store.get(str(ctx.session))
        active = [
            todo
            for todo in todos
            if todo.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
        ]
        if not active:
            return

        current_signature = _active_todo_hash(active)
        cached_signature = react_state.custom.get(TurnCustomKey.LAST_CONTINUATION_TODO_SIG)
        if cached_signature is not None and cached_signature == current_signature:
            return

        react_state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        react_state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] = current_signature
        reminder = (
            f"You have {len(active)} active todo tasks remaining. Use todo_read to see the full "
            "list.\n\n"
            "You should:\n"
            "- **Continue**: work on the next pending/in_progress todo item\n"
            "- **Cancel**: if the remaining tasks are no longer needed, mark them as cancelled "
            "with todo_write\n"
            "- **Acknowledge stuck**: if a task genuinely cannot be completed, leave its status "
            "unchanged and explain the blocker in your response — do NOT mark it completed unless "
            "it is actually done"
        )
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(reminder),
            }
        )
