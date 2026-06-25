from __future__ import annotations

import asyncio
import logging

from modex_agent.tools.overflow.models import CleanRequest
from modex_agent.tools.overflow.store import ToolOverflowStore

logger = logging.getLogger(__name__)

_KEPT_MERGE_WINDOW = 1.0


class OverflowCleaner:
    """Fire-and-forget overflow cleaner.

    Each ``schedule_cleanup`` spawns (or refreshes) a per-session timer.
    After *merge_window* seconds of silence the merged ``kept_call_ids``
    are flushed.  This keeps the store call synchronous while still
    merging rapid-fire overflow writes into one cleanup pass.
    """

    def __init__(
        self,
        store: ToolOverflowStore,
        *,
        merge_window: float = _KEPT_MERGE_WINDOW,
    ) -> None:
        self._store = store
        self._merge_window = merge_window
        self._pending: dict[str, CleanRequest] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    async def stop(self) -> None:
        for session_id in list(self._timers):
            self._cancel_timer(session_id)
        await self.flush()

    def schedule_cleanup(
        self,
        session_id: str,
        kept_call_ids: set[str],
        max_tool_call_ids: int = 500,
    ) -> None:
        existing = self._pending.get(session_id)
        if existing is not None:
            existing.kept_call_ids.update(kept_call_ids)
            existing.max_tool_call_ids = max(existing.max_tool_call_ids, max_tool_call_ids)
        else:
            self._pending[session_id] = CleanRequest(session_id, kept_call_ids, max_tool_call_ids)
        self._cancel_timer(session_id)
        loop = asyncio.get_running_loop()
        self._timers[session_id] = loop.call_later(
            self._merge_window,
            self._on_timer,
            session_id,
        )

    async def flush(self) -> None:
        for session_id in list(self._pending):
            self._cancel_timer(session_id)
            await self._clean_pending(session_id)

    def _on_timer(self, session_id: str) -> None:
        self._timers.pop(session_id, None)
        asyncio.create_task(
            self._clean_pending(session_id),
            name=f"overflow-clean-{session_id}",
        )

    def _cancel_timer(self, session_id: str) -> None:
        timer = self._timers.pop(session_id, None)
        if timer is not None:
            timer.cancel()

    async def _clean_pending(self, session_id: str) -> None:
        req = self._pending.pop(session_id, None)
        if req is None:
            return
        try:
            count = await self._store.clean(req)
            if count:
                logger.debug("Cleaned %d overflow entries for session=%s", count, session_id)
        except Exception:
            logger.warning("OverflowCleaner failed for session=%s", session_id, exc_info=True)
