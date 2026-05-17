from __future__ import annotations

import asyncio
import contextlib
import logging

from framework.tools.overflow.models import CleanRequest
from framework.tools.overflow.store import ToolOverflowStore

logger = logging.getLogger(__name__)

_DEFAULT_MERGE_WINDOW = 2.0
_BATCH_SIZE = 100


class OverflowCleaner:
    def __init__(
        self,
        store: ToolOverflowStore,
        *,
        merge_window: float = _DEFAULT_MERGE_WINDOW,
    ) -> None:
        self._store = store
        self._merge_window = merge_window
        self._queue: asyncio.Queue[CleanRequest | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = asyncio.create_task(self._loop(), name="overflow-cleaner")

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._worker, timeout=10.0)
        except TimeoutError:
            logger.warning("OverflowCleaner worker did not stop within 10s, cancelling")
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        self._worker = None

    def schedule_cleanup(self, session_id: str, kept_call_ids: set[str], max_tool_call_ids: int = 500) -> None:
        if self._worker is None or self._worker.done():
            logger.warning("Dropping cleanup request: worker not running")
            return
        self._queue.put_nowait(CleanRequest(session_id, kept_call_ids, max_tool_call_ids))

    async def _loop(self) -> None:
        pending: dict[str, CleanRequest] = {}

        async def _flush() -> None:
            if not pending:
                return
            batch = list(pending.values())
            pending.clear()
            for req in batch:
                try:
                    await self._clean_one(req)
                except Exception:
                    logger.warning("OverflowCleaner failed for session=%s", req.session_id, exc_info=True)

        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._merge_window)
            except TimeoutError:
                await _flush()
                continue

            if item is None:
                await _flush()
                return

            existing = pending.get(item.session_id)
            if existing is not None:
                existing.kept_call_ids.update(item.kept_call_ids)
                existing.max_tool_call_ids = max(existing.max_tool_call_ids, item.max_tool_call_ids)
            else:
                pending[item.session_id] = item

            if len(pending) >= _BATCH_SIZE:
                await _flush()

    async def flush(self) -> None:
        """Force-flush any pending cleanup requests (useful in tests)."""
        # Put a sentinel and immediately remove it to trigger the flush path
        # Actually, the simplest way is to drain the queue and flush pending
        pending: dict[str, CleanRequest] = {}
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is None:
                continue
            existing = pending.get(item.session_id)
            if existing is not None:
                existing.kept_call_ids.update(item.kept_call_ids)
                existing.max_tool_call_ids = max(existing.max_tool_call_ids, item.max_tool_call_ids)
            else:
                pending[item.session_id] = item
        for req in pending.values():
            try:
                await self._clean_one(req)
            except Exception:
                logger.warning("OverflowCleaner flush failed for session=%s", req.session_id, exc_info=True)

    async def _clean_one(self, req: CleanRequest) -> None:
        count = await self._store.clean(req)
        if count:
            logger.debug("Cleaned %d overflow entries for session=%s", count, req.session_id)
