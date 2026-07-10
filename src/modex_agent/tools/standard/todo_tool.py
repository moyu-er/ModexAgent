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

from modex_agent.core.agent import current_agent_context
from modex_agent.core.tool_manager import Tool
from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import TodoItem, TodoStore

#: The statuses that count as "active" (still to be done). Shared single source
#: of truth — ``todo_tool`` (dict view) and ``todo_probe`` (filter) both use it.
ACTIVE_TODO_STATUSES: tuple[TodoStatus, ...] = (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)

#: Guidance prefix prepended to the active-view JSON in every tool result, so the
#: model treats the list as an ordered work queue rather than a bare data dump.
#: Shared by ``todo_write``/``todo_read`` (and therefore by the probe hook, whose
#: injected ``todo_read`` call produces the same result).
ACTIVE_VIEW_PREFIX = (
    "Current unfinished tasks — work through them in the listed order "
    "(mark each in_progress when you start it, completed/cancelled when done):"
)


def _active_view(items: list[TodoItem]) -> list[dict[str, str]]:
    """Return the active subset (pending + in_progress) as plain dicts, in order.

    Single source of truth for what the agent and any consumer see: todo_write
    returns this for confirmation and todo_read returns this. Completed/cancelled
    items stay in the store but are never surfaced.
    """
    return [t.to_dict() for t in items if t.status in ACTIVE_TODO_STATUSES]


def _active_view_text(items: list[TodoItem]) -> str:
    """Render the active view as the tool-result string: the guidance prefix
    followed by the active items as JSON. Returns bare ``"[]"`` when there are
    no active items (nothing to direct the agent to, so no prefix).
    """
    active = _active_view(items)
    if not active:
        return "[]"
    return f"{ACTIVE_VIEW_PREFIX}\n{json.dumps(active, ensure_ascii=False)}"


def _resolve_session_id() -> str | None:
    """Return the current session id from the active agent context, if any."""
    ctx = current_agent_context.get(None)
    if ctx is None:
        return None
    return ctx.session.session_id


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
            "Plan and track multi-step work as a structured, evolving checklist.\n"
            "\n"
            "## When to use\n"
            "Use proactively when:\n"
            "- The task requires 3+ distinct steps or actions (not just 3 tool calls for one "
            "conceptual step).\n"
            "- The work is non-trivial and benefits from planning.\n"
            "- The user provides multiple tasks (numbered or comma-separated) or explicitly asks "
            "for a todo list.\n"
            "- New instructions arrive - capture them as todos.\n"
            "- You start a task - mark it `in_progress` (only one at a time) before working.\n"
            "- You finish a task - mark it `completed` and add any follow-ups discovered during "
            "the work.\n"
            "\n"
            "## When NOT to use\n"
            "Skip when:\n"
            "- The work is a single, straightforward task (or <3 trivial steps).\n"
            "- The request is purely informational or conversational.\n"
            "- Tracking adds no organizational value.\n"
            "\n"
            "## States\n"
            "- `pending` - not started.\n"
            "- `in_progress` - actively working (exactly ONE at a time).\n"
            "- `completed` - finished successfully.\n"
            "- `cancelled` - no longer needed.\n"
            "\n"
            "## Rules\n"
            "- **Completion protocol (most important):** the instant a task is done — BEFORE you "
            "summarize or end the turn — call `todo_write` to mark it `completed` and promote the "
            "next `pending` item to `in_progress`. Never claim completion in prose without updating "
            "the list first.\n"
            "- Mark `completed` only after the work is actually done, including any required "
            "verification. Never based on intent.\n"
            "- Keep exactly one `in_progress` at a time while work remains.\n"
            "- Update in real time — each finished item triggers an immediate `todo_write`; do not "
            "batch completions.\n"
            "- If blocked or partial, keep it `in_progress` and add a follow-up todo describing "
            "the blocker.\n"
            "- Preserve user-provided commands verbatim (flags, args, order).\n"
            "- Items should be specific and actionable; break large work into smaller steps.\n"
            "- Full-replace: every call sends the COMPLETE list. The store replaces, it does not "
            "merge. Omitting an item deletes it; reordering the array reorders execution.\n"
            "\n"
            "## Examples\n"
            "\n"
            "Use it:\n"
            '- "Add a dark mode toggle and run the tests" -> multi-step feature + explicit '
            "verification.\n"
            '- "Rename getCwd -> getCurrentWorkingDirectory across the repo" -> grep reveals 15 '
            "occurrences in 8 files.\n"
            '- "Implement registration, catalog, cart, checkout" -> multiple complex features.\n'
            "\n"
            "Skip it:\n"
            '- "How do I print Hello World in Python?" -> informational.\n'
            '- "Add a comment to calculateTotal" -> single edit.\n'
            '- "Run npm install and tell me what happened" -> one command.\n'
            "\n"
            "When in doubt, use it."
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

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401
        session_id = _resolve_session_id()
        if session_id is None:
            return "Error: no active agent session."
        items, err = _parse_todos(kwargs.get("todos", []))
        if err is not None:
            return f"Error: {err}"
        await self._store.save(session_id, items)
        return _active_view_text(items)


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
            "Read the active task list (pending + in_progress items), in execution order.\n"
            "\n"
            "## When to call\n"
            "- At the START of any resume / continue / 'try again' turn — before doing anything "
            "else. This is the single most important time to call it.\n"
            "- When you are unsure what to do next or where you left off.\n"
            "- When the list may be stale (you have done work since the last read but have not "
            "updated it with todo_write).\n"
            "- Before ending a turn, if you suspect there may be unfinished work.\n"
            "\n"
            "## What it returns\n"
            "Only the active subset (pending + in_progress), in the order they should be executed. "
            "Completed and cancelled items are hidden — they are retained in the store but never "
            "re-surfaced here.\n"
            "\n"
            "## After reading\n"
            "- If the `in_progress` item is still genuinely in progress, continue it.\n"
            "- If you have actually finished it since the last update, call todo_write to mark it "
            "`completed` and promote the next `pending` to `in_progress`.\n"
            "- If the list no longer reflects reality (wrong order, missing steps, stale items), "
            "call todo_write with the corrected full list."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:  # noqa: ANN401
        session_id = _resolve_session_id()
        if session_id is None:
            return "Error: no active agent session."
        all_items = await self._store.get(session_id)
        return _active_view_text(all_items)
