"""todo_write / todo_read — per-session task list tools.

Two independent tools (read/write split, like Claude Code's Task* family).
The ``TodoStore`` is injected via the constructor at registration time (mirrors
ExperienceTool). The session id and emitter come from ``current_agent_context``.
State reaches the model via tool results in history; no prompt injection (v1).
"""

from __future__ import annotations

import json
from typing import Any

from framework.core.agent import current_agent_context
from framework.core.tool_manager import Tool
from framework.core.types import TodoStatus
from framework.runtime.store import TodoItem, TodoStore

_ACTIVE_STATUSES = (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)


def _resolve_session_and_emitter() -> tuple[str | None, Any]:
    """Return (session_id, emitter) from the active agent context."""
    ctx = current_agent_context.get(None)
    if ctx is None:
        return None, None
    session_id = getattr(ctx.session, "session_id", None)
    emitter = getattr(ctx, "emitter", None)
    return session_id, emitter


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
            "Maintain a structured task list for the current session. Use proactively "
            "when the task has 3+ distinct steps, when the user gives multiple tasks, "
            "or when new instructions arrive. When NOT to use: a single straightforward "
            "task, or a purely informational request.\n\n"
            "ORDER MATTERS: the list order IS the intended execution sequence. Maintain a "
            "meaningful order and work through items top-to-bottom.\n\n"
            "Rules: update the list in real time, do not batch; mark an item 'completed' "
            "only AFTER the work is truly done including verification, never on intent; if "
            "blocked, keep it 'in_progress' and add a follow-up item describing the blocker; "
            "keep items specific and actionable; preserve user-provided commands verbatim. "
            "Completed/cancelled items are NOT shown to the user or returned by todo_read, so "
            "you may drop them once done to keep the list focused.\n\n"
            "Full-replace semantics: send the ENTIRE updated list every call."
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
        session_id, emitter = _resolve_session_and_emitter()
        if session_id is None:
            return "Error: no active agent session."
        items, err = _parse_todos(todos)
        if err is not None:
            return f"Error: {err}"
        await self._store.save(session_id, items)
        payload = [t.to_dict() for t in items]
        if emitter is not None:
            await emitter.emit("todo.updated", {"session_id": session_id, "todos": payload})
        return json.dumps(payload, ensure_ascii=False)


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
            "Return the ACTIVE tasks only (pending + in_progress) for the current session, "
            "in execution order. Completed/cancelled items are excluded. Call this when you "
            "are unsure what you are currently working on, or when the list may be stale "
            "(e.g. after a long conversation or context compression)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        session_id, _emitter = _resolve_session_and_emitter()
        if session_id is None:
            return "Error: no active agent session."
        all_items = await self._store.get(session_id)
        active = [t for t in all_items if t.status in _ACTIVE_STATUSES]
        return json.dumps([t.to_dict() for t in active], ensure_ascii=False)
