from __future__ import annotations

from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.models import OverflowRef
from modex_agent.tools.overflow.store import ToolOverflowStore


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
    ) -> None:
        self._store = store
        self._cleaner = cleaner

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
        truncated_text = content[:max_chars] + (
            f"\n\n[Full output ({ref.total_chars} chars total) saved to: "
            f"{ref.dir_path}/full.txt]"
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
