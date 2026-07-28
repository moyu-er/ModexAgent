"""Dynamic behavior injectors for the ReAct loop.

Two components:

- :class:`TodoListReminderInjector` — a ``BeforeIterationHook`` that reminds
  the agent to update its todo list if it hasn't done so recently
  (interval-based, capped per turn).

- :class:`PostCompactionRefreshInjector` — a ``BeforeTurnHook`` +
  ``MemoryCleanupListener`` combo that reacts to **real cleanup events**
  (session pruning) and injects a re-orientation ``<system-reminder>``
  immediately after compaction completes.  When a :class:`TodoStore` is
  provided and has active items, the todo list is included inline so the
  agent can resume work without calling ``todo_read`` first.

  Design rationale:

  * **Event-driven, not heuristic** — ``on_cleanup_finished`` fires inside
    ``append()`` which is inside the ReAct iteration that produced the
    message, so the reminder is visible to the *next* iteration's LLM call
    (or to the next turn if the cleanup happened on the last append of a
    turn).
  * **No state to lose** — the reminder is a persisted user message in
    history, not a transient flag.  If the turn ends right after compaction,
    the next turn sees the reminder in history.
  * **Cross-turn safety** — cleanup triggered between turns (pipeline-layer
    ``append``) has no ``current_agent_context``, so the listener skips
    injection.  That is correct: the new turn will see the pruned history
    directly.
  * **Re-entrancy guard** — a ``_injecting`` flag prevents infinite
    recursion when the reminder's own ``append`` triggers another cleanup.
  * **Hook-based configuration** — register via ``hook_runner`` like any
    other ``BeforeTurnHook``.  The hook auto-registers itself as a cleanup
    listener on the session's ``ScopedMessageHistory`` at turn start.
  * **No todo → no injection** — when ``todo_store`` is ``None`` or has no
    active items, only the compaction notice is injected (no empty todo
    section).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.core.agent import current_agent_context
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.hook.abc import BeforeIterationHook, BeforeTurnHook
from modex_agent.memory.cleanup_events import MemoryCleanupListener
from modex_agent.runtime.store import TodoItem, TodoStore

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.scope import MemoryContext
    from modex_agent.memory.cleanup import CleanupResult
    from modex_agent.memory.core.models import CompressionReason

logger = logging.getLogger(__name__)

#: Transient key in ``state.custom`` for todo-reminder bookkeeping.
_TODO_REMINDER_KEY = "_todo_reminder_state"

#: Maximum number of history instances to track (prevents unbounded growth).
_MAX_TRACKED_HISTORIES = 128


class TodoListReminderInjector(BeforeIterationHook):
    """[DEPRECATED] Inject a reminder to update the todo list if it hasn't been touched recently.

    Superseded by ``CompactionReminderHook`` (``modex_agent.hook.builtin``).
    This injector was never wired in production and uses a redundant dual-role
    design. Do not register in new code.

    Runs as a ``BeforeIterationHook``. Every ``reminder_interval`` iterations,
    if the todo store has active (pending/in_progress) items, a
    ``<system-reminder>`` user message is appended to history. At most
    ``max_reminders`` reminders are injected per turn.

    Per-turn bookkeeping (``last_reminder_iteration``, ``reminders_sent``)
    lives in ``ctx.runtime.state.custom`` under ``_todo_reminder_state``,
    so it resets each turn.
    """

    def __init__(
        self,
        todo_store: TodoStore,
        *,
        reminder_interval: int = 10,
        max_reminders: int = 3,
    ) -> None:
        self._todo_store = todo_store
        self._reminder_interval = max(1, reminder_interval)
        self._max_reminders = max(0, max_reminders)

    @property
    def name(self) -> str:
        return "todo_list_reminder"

    async def before_iteration(self, ctx: AgentContext) -> None:
        from modex_agent.agents.react.state import get_react_state

        state = get_react_state(ctx)
        if state is None:
            return

        iteration = state.iteration
        reminder_state: dict[str, int] = state.custom.get(_TODO_REMINDER_KEY, {})
        last_reminder_iteration = reminder_state.get("last_reminder_iteration", 0)
        reminders_sent = reminder_state.get("reminders_sent", 0)

        # Cap reached — don't over-remind.
        if reminders_sent >= self._max_reminders:
            return

        # Too soon since the last reminder.
        if iteration - last_reminder_iteration < self._reminder_interval:
            return

        # Read the todo list and check for active items.
        session_id = str(ctx.session)
        items: list[TodoItem] = await self._todo_store.get(session_id)
        active = [t for t in items if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)]
        if not active:
            return

        todo_lines = [f"  - [{t.status.value}] {t.content}" for t in active]
        todo_text = "\n".join(todo_lines)
        reminder = (
            "<system-reminder>"
            "The todo list has not been updated recently. "
            f"Current active todos:\n{todo_text}\n"
            "If your tasks have changed, update the list using todo_write. "
            "Never mention this reminder to the user."
            "</system-reminder>"
        )
        await ctx.history.append(ChatMessage(role=MessageRole.USER, content=reminder))

        # Update per-turn bookkeeping.
        reminder_state["last_reminder_iteration"] = iteration
        reminder_state["reminders_sent"] = reminders_sent + 1
        state.custom[_TODO_REMINDER_KEY] = reminder_state


class PostCompactionRefreshInjector(BeforeTurnHook, MemoryCleanupListener):
    """[DEPRECATED] Inject a re-orientation reminder after context compaction.

    Superseded by ``CompactionReminderHook`` (``modex_agent.hook.builtin``).
    This injector was never wired in production and uses a redundant dual-role
    design (BeforeTurnHook + MemoryCleanupListener) with contextvar hacks.
    Do not register in new code.

    Dual-role component:

    - **``BeforeTurnHook``** — at turn start, auto-registers itself as a
      ``MemoryCleanupListener`` on the session's ``ScopedMessageHistory``
      so that compaction events during this turn trigger injection.
    - **``MemoryCleanupListener``** — ``on_cleanup_finished`` appends a
      ``<system-reminder>`` to history via ``current_agent_context``,
      including the active todo list when a :class:`TodoStore` is provided.

    Registration is hook-based: add it to ``hook_runner`` like any other
    ``BeforeTurnHook``.  No changes to pipeline or memory layers needed.

    When ``todo_store`` is ``None`` or has no active items, only the
    compaction notice is injected (no empty todo section).
    """

    def __init__(self, todo_store: TodoStore | None = None) -> None:
        self._todo_store = todo_store
        self._registered_histories: set[int] = set()
        self._injecting: bool = False

    @property
    def name(self) -> str:
        return "post_compaction_refresh"

    # ------------------------------------------------------------------
    # BeforeTurnHook — auto-register as cleanup listener
    # ------------------------------------------------------------------

    async def before_turn(self, ctx: AgentContext) -> None:
        """Register self as a cleanup listener on the session history."""
        history = ctx.history
        hist_id = id(history)
        if hist_id in self._registered_histories:
            return  # Already registered for this history instance

        # ScopedMessageHistory holds _cleanup_listeners list.
        listeners = getattr(history, "_cleanup_listeners", None)
        if listeners is not None and self not in listeners:
            listeners.append(self)
        self._registered_histories.add(hist_id)

        # Evict oldest entries to prevent unbounded growth.
        if len(self._registered_histories) > _MAX_TRACKED_HISTORIES:
            oldest = next(iter(self._registered_histories))
            self._registered_histories.discard(oldest)

    # ------------------------------------------------------------------
    # MemoryCleanupListener
    # ------------------------------------------------------------------

    async def on_cleanup_triggered(self, context: MemoryContext, reason: CompressionReason) -> None:
        """Pre-archive notification — not used for injection."""
        pass

    async def on_cleanup_finished(self, context: MemoryContext, result: CleanupResult) -> None:
        """Post-cleanup injection of re-orientation reminder."""
        if not result.triggered or result.messages_pruned == 0:
            return

        # Re-entrancy guard: the reminder's own append triggers another
        # cleanup check.  If that cleanup fires (unlikely for a single
        # small message, but possible at the threshold boundary), skip
        # to prevent infinite recursion.
        if self._injecting:
            return

        # Only inject when an agent turn is in progress (context var is set).
        # Between-turn cleanups (pipeline layer) have no active agent context;
        # the next turn sees the pruned history directly.
        ctx = current_agent_context.get(None)
        if ctx is None:
            return

        parts: list[str] = [
            "<system-reminder>"
            "Context was recently compacted to free up space. "
            "Earlier conversation messages have been pruned and archived. "
            "The pruned transcript catalog has references to earlier "
            "conversation if you need details."
        ]

        # Inject the current active todo list so the agent can re-orient
        # without an extra todo_read call.
        if self._todo_store is not None:
            session_id = str(ctx.session)
            try:
                items: list[TodoItem] = await self._todo_store.get(session_id)
                active = [
                    t for t in items if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
                ]
                if active:
                    todo_lines = [f"  - [{t.status.value}] {t.content}" for t in active]
                    parts.append(
                        "Your current active todos (injected from todo store):\n"
                        + "\n".join(todo_lines)
                        + "\nUse todo_read to see the full list, and todo_write "
                        "to update if tasks have changed."
                    )
            except (OSError, RuntimeError, ValueError, KeyError):
                logger.warning(
                    "Failed to read todos during compaction refresh",
                    exc_info=True,
                )

        parts.append("</system-reminder>")
        reminder = "".join(parts)

        self._injecting = True
        try:
            await ctx.history.append(ChatMessage(role=MessageRole.USER, content=reminder))
        finally:
            self._injecting = False
