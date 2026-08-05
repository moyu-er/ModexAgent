"""Post-cleanup todo reorientation hook.

After session memory cleanup prunes messages, persists a single
``<system-reminder>`` USER message via ``SessionMemoryManager.add_messages``
so the agent can re-orient on its next iteration. Replaces the former
heuristic history-diff detection with an event-driven persisted reminder.

Persistence path: ``SessionMemoryManager.add_messages`` (Path A — no
``MemoryAppendRecorder``, no ``_run_cleanup`` re-entry, no ``write_id``).
This is the ONLY safe direct-persistence path; ``ScopedMessageHistory.append``
would re-enter cleanup and ``DefaultMemorySystem.add_messages`` would fan
out to ``MemoryProvider`` instances.
"""

from __future__ import annotations

import logging

from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.memory.hooks import CleanupFinishedHook, MemoryHookContext
from modex_agent.runtime.store import TodoItem, TodoStore

logger = logging.getLogger(__name__)


def _build_todo_section(active_todos: list[TodoItem]) -> str:
    if not active_todos:
        return ""
    todo_lines = [f"  - [{t.status.value}] {t.content}" for t in active_todos]
    return (
        "Your current active todos:\n"
        + "\n".join(todo_lines)
        + "\nUse todo_read to see the full list. Continue your work."
    )


def _build_reminder(
    *,
    has_archive: bool,
    todo_section: str | None,
) -> str:
    lines: list[str] = ["<system-reminder>"]
    lines.append("Earlier conversation context was compacted (earlier messages were pruned).")
    if has_archive:
        lines.append(
            "The archive summaries in your system prompt provide condensed "
            "overviews of earlier conversations — the one with the highest "
            "number is the most recent."
        )
    lines.append(
        "You can also review the pruned transcript catalog to recall "
        "specific details of what was discussed."
    )
    if todo_section is not None:
        lines.append("")
        lines.append(todo_section)
    else:
        lines.append("Continue your work.")
    lines.append("</system-reminder>")
    return "\n".join(lines)


class TodoReorientationHook(CleanupFinishedHook):
    """Persist a re-orientation reminder after cleanup prunes messages.

    Event-driven replacement for the former heuristic history-diff detection:
    instead of detecting cleanup by diffing history snapshots across
    iterations, this hook fires directly from the ``MemoryHookRunner``
    dispatch at ``MemoryHookPoint.CLEANUP_FINISHED``.

    Detection is purely ``cleanup_result.messages_pruned > 0`` — no
    heuristic length/fingerprint diffing. The reminder is persisted through
    ``SessionMemoryManager.add_messages`` (Path A — no recorder, no
    ``_run_cleanup``, no ``write_id`` set on the message).

    Configuration (immutable, injected at construction):
      - ``todo_store``: per-session todo persistence (``None`` if todo tools
        are absent from the agent).
      - ``has_archive``: whether archive summaries are injected into the
        system prompt (controls the archive-summaries paragraph in the
        reminder wording — NOT detection).
    """

    def __init__(
        self,
        todo_store: TodoStore | None,
        *,
        has_archive: bool = False,
    ) -> None:
        self._todo_store = todo_store
        self._has_archive = has_archive

    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None:
        result = ctx.cleanup_result
        if result is None or not result.triggered or result.messages_pruned == 0:
            return
        if ctx.session_manager is None or ctx.memory_context is None:
            return

        todo_section: str | None = None
        session_id = ctx.memory_context.session_id
        if self._todo_store is not None and session_id is not None:
            try:
                todos = await self._todo_store.get(session_id)
                active = [
                    t
                    for t in todos
                    if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
                ]
                if active:
                    todo_section = _build_todo_section(active)
            except Exception:
                logger.debug(
                    "Failed to read todos for reorientation reminder",
                    exc_info=True,
                )

        reminder = _build_reminder(
            has_archive=self._has_archive,
            todo_section=todo_section,
        )
        await ctx.session_manager.add_messages(
            ctx.memory_context,
            [ChatMessage(role=MessageRole.USER, content=reminder)],
        )
