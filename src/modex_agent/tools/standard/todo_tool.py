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
#: of truth — used by ``todo_tool`` and ``TodoReorientationHook``.
ACTIVE_TODO_STATUSES: tuple[TodoStatus, ...] = (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)

#: Guidance prefix prepended to the active-view JSON in every tool result, so the
#: model treats the list as an ordered work queue rather than a bare data dump.
#: Shared by ``todo_write``/``todo_read``.
ACTIVE_VIEW_PREFIX = (
    "Current unfinished tasks — work through them in the listed order "
    "(mark each in_progress when you start it, completed/cancelled when done):"
)

#: Finish-line guidance appended to the ``todo_write`` result when a
#: full-replace write completes the plan (>=1 completed item, none active).
#: Fires exactly at the natural end-of-work moment, where self-review
#: anchoring (re-reading your own intent instead of the artifact) is the
#: known failure mode. Not appended to all-cancelled or genuinely empty
#: lists (no work finished), nor to ``todo_read`` (a status query is not a
#: completion event).
FINISHED_VIEW_SUFFIX = (
    "All tasks completed. Before reporting done: re-verify each deliverable "
    "against the task statement (paths, formats, constraints). For "
    "deliverable files, prefer a fresh-eyes subagent review that decodes "
    "them from the specification — self-review re-reads its own intent, "
    "not the artifact."
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


def _plan_finished(items: list[TodoItem]) -> bool:
    """True when a full-replace write ends with finished work: the list is
    non-empty, nothing is still active, and at least one item completed.
    All-cancelled or empty lists are not finishes (work was dropped or
    never tracked), so they carry no finish-line reminder.
    """
    return bool(items) and not _active_view(items) and any(
        t.status is TodoStatus.COMPLETED for t in items
    )


def _write_view_text(items: list[TodoItem]) -> str:
    """``todo_write``'s result: the active view plus the finish-line reminder
    when this write completed the plan. ``todo_read`` keeps the bare active
    view — reading a finished list is a status query, not the completion
    event the reminder addresses.
    """
    base = _active_view_text(items)
    if _plan_finished(items):
        return f"{base}\n{FINISHED_VIEW_SUFFIX}"
    return base


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
            "Create and maintain a structured task list for the current "
            "session. Tracks progress, organizes multi-step work, and "
            "surfaces status to the user.\n"
            "\n"
            "## When to use\n"
            "- The task requires 3+ distinct steps or actions.\n"
            "- The work is non-trivial and benefits from planning.\n"
            "- You are mid-task and more work remains than expected — "
            "start tracking now (done steps as `completed`).\n"
            "- The user provides multiple tasks or explicitly asks for a "
            "todo list.\n"
            "\n"
            "## When NOT to use\n"
            "- Single straightforward task or <3 trivial steps.\n"
            "- Purely informational or conversational.\n"
            "\n"
            "## States\n"
            "- `pending` — not started.\n"
            "- `in_progress` — actively working (exactly ONE at a time).\n"
            "- `completed` — finished successfully.\n"
            "- `cancelled` — no longer needed.\n"
            "\n"
            "## Rules\n"
            "- Prefer creating the full list as `pending` before starting "
            "any work; if already underway, snapshot progress (done steps "
            "as `completed`) and plan the remainder.\n"
            "- End multi-step plans with an explicit final verification "
            "step: re-check every deliverable against the task statement "
            "itself (paths, names, formats, constraints) — not against "
            "memory. For high-stakes deliverables, make that step a "
            "fresh-eyes subagent review instead of self-review.\n"
            "- Mark `completed` only after the work is actually done, "
            "including verification. Never based on intent.\n"
            "- Keep exactly one `in_progress` while work remains.\n"
            "- Update in real time; do not batch completions.\n"
            "- If blocked, keep `in_progress` and add a follow-up todo "
            "describing the blocker.\n"
            "- Items should be specific and actionable.\n"
            "- Full-replace: every call sends the COMPLETE list. Omitting "
            "an item deletes it; reordering reorders execution.\n"
            "\n"
            "## Examples\n"
            "Use it:\n"
            '- "Add a dark mode toggle and run the tests" -> multi-step + '
            "verification\n"
            '- "Rename getCwd -> getCurrentWorkingDirectory across the repo" '
            "-> 15 occurrences in 8 files\n"
            '- Mid-task: "3 more files need the same fix" -> start '
            "tracking now, first items marked `completed`\n"
            "Skip it:\n"
            '- "How do I print Hello World in Python?" -> informational\n'
            '- "Add a comment to calculateTotal" -> single edit\n'
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
        return _write_view_text(items)


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
            "Read the active task list (pending + in_progress items), in "
            "execution order. Call this at the start of any resume / "
            "continue turn to recover your place."
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
