"""CompactionReminderHook — inject a re-orientation reminder after session cleanup.

Detects session memory cleanup (pruning) within the current turn by comparing
the history length and first-message fingerprint against the previous
iteration's snapshot. When cleanup is detected, appends a single
``<system-reminder>`` user message to history so the agent can re-orient.

Detection signals (any one triggers):
  - Length dropped below 50% of the previous snapshot.
  - Length dropped by more than 20 messages.
  - First message fingerprint (role + content prefix) changed.

Known limitations (documented for future maintainers):
  - Per-turn scope: cleanup on the last iteration of a turn cannot be detected
    by the next turn because ``state.custom`` is destroyed between turns.
    A persistence-backed version can close this gap.
  - Archive-only cleanup (no pruning) that does not change the first message
    will not be detected.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.core.message import ChatMessage, TextPart
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.hook.abc import BeforeIterationHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.store import TodoItem, TodoStore

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

_FINGERPRINT_PREFIX_LEN = 64
_LENGTH_DROP_RATIO = 0.5
_LENGTH_DROP_ABSOLUTE = 20


def _fingerprint(msg: ChatMessage) -> str:
    content = msg.content
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        text = " ".join(p.text for p in content if isinstance(p, TextPart))
    return f"{msg.role}|{text[:_FINGERPRINT_PREFIX_LEN]}"


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


def _build_todo_section(active_todos: list[TodoItem]) -> str:
    if not active_todos:
        return ""
    todo_lines = [f"  - [{t.status.value}] {t.content}" for t in active_todos]
    return (
        "Your current active todos:\n"
        + "\n".join(todo_lines)
        + "\nUse todo_read to see the full list. Continue your work."
    )


class CompactionReminderHook(BeforeIterationHook):
    """Inject a re-orientation reminder when session cleanup is detected.

    Stateless hook — all per-turn state lives in ``ctx.runtime.state.custom``
    under ``TurnCustomKey.COMPACTION_PREV_SNAPSHOT``. The hook compares the
    current history snapshot against the previous iteration's snapshot to
    detect cleanup. When detected, a single reminder is appended to history.

    Configuration is injected at construction time (pool assembly):
      - ``todo_store``: per-session todo persistence (None if todo tools absent)
      - ``has_todo_tool``: whether ``todo_read``/``todo_write`` are registered
      - ``has_archive``: whether archive summaries are injected into the
        system prompt (controls the archive mention in the reminder)
    """

    def __init__(
        self,
        *,
        todo_store: TodoStore | None = None,
        has_todo_tool: bool = False,
        has_archive: bool = False,
    ) -> None:
        self._todo_store = todo_store
        self._has_todo_tool = has_todo_tool
        self._has_archive = has_archive

    @property
    def name(self) -> str:
        return "compaction_reminder"

    async def before_iteration(self, ctx: AgentContext) -> None:
        from modex_agent.agents.react.state import get_react_state

        state = get_react_state(ctx)
        if state is None:
            return

        history = await ctx.history.to_list()
        current_len = len(history)
        current_fp = _fingerprint(history[0]) if history else None

        prev = state.custom.get(TurnCustomKey.COMPACTION_PREV_SNAPSHOT)
        if prev is not None:
            prev_len = prev.get("len", 0)
            prev_fp = prev.get("fp")
            if self._is_cleanup_detected(current_len, prev_len, current_fp, prev_fp):
                await self._inject_reminder(ctx)
                history = await ctx.history.to_list()
                current_len = len(history)
                current_fp = _fingerprint(history[0]) if history else None

        state.custom[TurnCustomKey.COMPACTION_PREV_SNAPSHOT] = {
            "len": current_len,
            "fp": current_fp,
        }

    @staticmethod
    def _is_cleanup_detected(
        current_len: int,
        prev_len: int,
        current_fp: str | None,
        prev_fp: str | None,
    ) -> bool:
        if current_len < prev_len * _LENGTH_DROP_RATIO:
            return True
        if prev_len - current_len > _LENGTH_DROP_ABSOLUTE:
            return True
        return current_fp is not None and prev_fp is not None and current_fp != prev_fp

    async def _inject_reminder(self, ctx: AgentContext) -> None:
        todo_section: str | None = None
        if self._has_todo_tool and self._todo_store is not None:
            try:
                session_id = str(ctx.session)
                items = await self._todo_store.get(session_id)
                active = [
                    t for t in items if t.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
                ]
                if active:
                    todo_section = _build_todo_section(active)
            except Exception:
                logger.debug("Failed to read todos for compaction reminder", exc_info=True)

        reminder = _build_reminder(
            has_archive=self._has_archive,
            todo_section=todo_section,
        )
        await ctx.history.append(ChatMessage(role=MessageRole.USER, content=reminder))
