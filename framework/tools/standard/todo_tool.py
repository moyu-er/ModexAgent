"""todo_write / todo_read — per-session task list tools.

Two independent tools (read/write split, like Claude Code's Task* family).
The ``TodoStore`` is injected via the constructor at registration time (mirrors
ExperienceTool); only the session id is read from ``current_agent_context``.

The tool is intentionally decoupled from the frontend: it does NOT emit any
presentation event. Its return value (the active list JSON) flows to clients as
a normal tool result, and the WebUI derives the task panel from that. State
reaches the model via tool results in history; no prompt injection (v1).
"""

from __future__ import annotations

import json
from typing import Any

from framework.core.agent import current_agent_context
from framework.core.tool_manager import Tool
from framework.core.types import TodoStatus
from framework.runtime.store import TodoItem, TodoStore

_ACTIVE_STATUSES = (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)


def _active_view(items: list[TodoItem]) -> list[dict[str, str]]:
    """Return the active subset (pending + in_progress) as plain dicts, in order.

    Single source of truth for what the agent and any consumer see: todo_write
    returns this for confirmation and todo_read returns this. Completed/cancelled
    items stay in the store but are never surfaced.
    """
    return [t.to_dict() for t in items if t.status in _ACTIVE_STATUSES]


def _resolve_session_id() -> str | None:
    """Return the current session id from the active agent context, if any."""
    ctx = current_agent_context.get(None)
    if ctx is None:
        return None
    return getattr(ctx.session, "session_id", None)


def _parse_todos(raw: list[dict[str, Any]]) -> tuple[list[TodoItem], str | None]:
    """Parse raw LLM todo dicts into TodoItem. Returns (items, error_or_None)."""
    if not isinstance(raw, list):
        return [], "todos must be an array"
    items: list[TodoItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return [], f"invalid todo entry: {entry!r}"
        content = entry.get("content")
        status_raw = entry.get("status")
        if not isinstance(content, str) or not isinstance(status_raw, str):
            return [], f"invalid todo entry (need content+status): {entry!r}"
        try:
            status = TodoStatus(status_raw)
        except ValueError:
            return [], (
                f"invalid status {status_raw!r}; expected one of "
                f"{[s.value for s in TodoStatus]}"
            )
        items.append(TodoItem(content=content, status=status))
    return items, None


class TodoWriteTool(Tool):
    """Full-replace write of the session task list."""

    def __init__(self, store: TodoStore) -> None:
        super().__init__()
        self._store = store

    @property
    def name(self) -> str:
        return "todo_write"

    @property
    def description(self) -> str:
        return (
            "Maintain a structured task list for this session to track multi-step work. "
            "The list and its order are shown to the user.\n\n"
            "Use when the task has 3+ distinct steps or the user gives multiple tasks. "
            "Skip for single, trivial, or purely informational requests.\n\n"
            "Statuses: pending, in_progress (more than one allowed), completed, cancelled.\n"
            "- ORDER MATTERS: list order is the execution sequence — work top to bottom.\n"
            "- Update in real time; don't batch.\n"
            "- Mark completed only after the work is truly done AND verified — never on intent.\n"
            "- Blocked? Keep it in_progress and add a follow-up item describing the blocker.\n"
            "- Full-replace: send the entire list every call. Returns the active items "
            "(in_progress + pending) so you can confirm what remains; completed/cancelled "
            "are excluded from the return and may be dropped."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        }

    async def execute(self, todos: list[dict[str, Any]], **kwargs: Any) -> str:
        session_id = _resolve_session_id()
        if session_id is None:
            return "Error: no active agent session."
        items, err = _parse_todos(todos)
        if err is not None:
            return f"Error: {err}"
        await self._store.save(session_id, items)
        return json.dumps(_active_view(items), ensure_ascii=False)


class TodoReadTool(Tool):
    """Read the active (pending + in_progress) task list, in execution order."""

    def __init__(self, store: TodoStore) -> None:
        super().__init__()
        self._store = store

    @property
    def name(self) -> str:
        return "todo_read"

    @property
    def description(self) -> str:
        return (
            "Return the ACTIVE task list (pending + in_progress) for this session, in "
            "execution order; completed/cancelled are excluded. Call when you're unsure "
            "what you're working on, or after context compression/archiving may have made "
            "the list stale."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        session_id = _resolve_session_id()
        if session_id is None:
            return "Error: no active agent session."
        all_items = await self._store.get(session_id)
        return json.dumps(_active_view(all_items), ensure_ascii=False)
