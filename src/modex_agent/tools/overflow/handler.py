from __future__ import annotations

from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.models import OverflowRef
from modex_agent.tools.overflow.store import ToolOverflowStore
from modex_agent.tools.overflow.truncate import (
    DEFAULT_HEAD_RATIO,
    DEFAULT_TAIL_RATIO,
    render_overflow_text,
    split_head_tail,
)


class ToolResultOverflowHandler:
    """Orchestrates overflow: store full content, return truncated text with path notification.

    The truncation limit is passed by the caller (interceptor), not owned by
    this handler. This ensures a single source of truth for the overflow
    threshold — the interceptor decides both when to trigger overflow and
    how much content to return.
    """

    def __init__(
        self,
        store: ToolOverflowStore,
        cleaner: OverflowCleaner,
        *,
        head_ratio: float = DEFAULT_HEAD_RATIO,
        tail_ratio: float = DEFAULT_TAIL_RATIO,
    ) -> None:
        self._store = store
        self._cleaner = cleaner
        self._head_ratio = head_ratio
        self._tail_ratio = tail_ratio

    async def store_overflow(
        self,
        session_id: str,
        tool_call_id: str,
        tool_name: str,
        content: str,
        *,
        max_chars: int = 50_000,
    ) -> tuple[str, OverflowRef]:
        ref = await self._store.store(session_id, tool_call_id, tool_name, content)
        # Content at or under the threshold passes through unchanged — the
        # gate is explicit because the head/tail fractions no longer sum to
        # the threshold (10% + 15%), so the render's own under-budget check
        # cannot provide this pass-through.
        if len(content) <= max_chars:
            return content, ref
        head_chars, tail_chars = split_head_tail(
            max_chars, self._head_ratio, self._tail_ratio
        )
        truncated_text = render_overflow_text(
            content,
            head_chars=head_chars,
            tail_chars=tail_chars,
            full_output_path=f"{ref.dir_path}/full.txt",
        )
        return truncated_text, ref

    def repoint_store(self, store: ToolOverflowStore) -> None:
        """Retarget this handler (and its cleaner) at a new overflow store.

        Used by workspace switching to point overflow at the active workspace's
        overflow directory without reconstructing the interceptor chain.
        """
        self._store = store
        if self._cleaner is not None:
            self._cleaner._store = store

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str]) -> None:
        self._cleaner.schedule_cleanup(session_id, kept_call_ids)
