"""Auto-compaction for idle sessions.

Periodically scans scopes and compacts old messages for sessions
that have been idle beyond a threshold. Uses MemoryCompactionPipeline
so that idle compaction and token-pressure compaction share the same
message policy, boundary rules, summary strategy, and archive format.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from framework.memory.compaction.pipeline import MemoryCompactionPipeline
from framework.memory.core.scope import MemoryAgentRole, MemoryLayerName, ScopeRecord
from framework.memory.core.storage import MemoryStorage

logger = logging.getLogger(__name__)


class AutoCompactService:
    """Idle session auto-compaction service.

    Scopes are discovered via ``storage.list_scope_records()``. For each idle
    scope, the service runs ``MemoryCompactionPipeline`` (if provided) or falls
    back to a simple tail-keep truncation.
    """

    def __init__(
        self,
        storage: MemoryStorage,
        idle_threshold_seconds: float = 1800.0,
        keep_recent_messages: int = 8,
        history_manager: Any | None = None,
        archive_strategy: Any | None = None,
        pipeline: MemoryCompactionPipeline | None = None,
    ) -> None:
        self._storage = storage
        self._idle_threshold = idle_threshold_seconds
        self._keep_recent = keep_recent_messages
        self._history_manager = history_manager
        self._archive_strategy = archive_strategy
        self._pipeline = pipeline

    async def _list_scope_records(self) -> list[ScopeRecord]:
        """List main-agent short-term scopes that have message data."""
        return await self._storage.list_scope_records(
            layer=MemoryLayerName.SHORT_TERM,
            has_file="messages",
            agent_roles={MemoryAgentRole.MAIN},
        )

    async def scan_once(self) -> list[str]:
        """Run one scan-and-compact pass.

        Returns:
            List of scope_keys that were actually compacted.
        """
        compacted: list[str] = []
        for record in await self._list_scope_records():
            scope_key = record.scope_key
            try:
                if await self._is_idle(scope_key) and await self._compact(record):
                    compacted.append(scope_key)
            except Exception:
                logger.exception("Auto-compact failed for scope %s", scope_key)
        return compacted

    async def _is_idle(self, scope_key: str) -> bool:
        """Check whether a scope has been idle longer than the threshold."""
        last_activity = await self._storage.get(scope_key, ".last_activity")
        if isinstance(last_activity, int | float):
            return time.time() - last_activity > self._idle_threshold

        # Fallback: use .scope.json updated_at when .last_activity is missing
        records = await self._storage.list_scope_records(
            layer=MemoryLayerName.SHORT_TERM,
            agent_roles={MemoryAgentRole.MAIN},
        )
        for record in records:
            if record.scope_key == scope_key:
                if record.updated_at is not None:
                    return time.time() - record.updated_at > self._idle_threshold
                break

        return False

    async def _compact(self, record: ScopeRecord) -> bool:
        """Compact a single idle scope.

        The entire load-compact-save sequence runs inside the scope write lock
        so that new messages arriving during compaction are not lost.  The
        underlying lock is writer-reentrant, so nested acquisitions by
        ``load_messages``, ``save_messages`` and ``set`` are safe.

        Returns:
            True if compaction actually happened.
        """
        scope_key = record.scope_key
        async with self._storage.get_lock(scope_key).write():
            # Double-check idle inside the lock.  Another task may have
            # appended a message (and updated .last_activity) between the
            # _is_idle() call in scan_once() and this point.
            last_activity = await self._storage.get(scope_key, ".last_activity")
            if isinstance(last_activity, int | float):
                if time.time() - last_activity <= self._idle_threshold:
                    return False
            else:
                # Fallback: read updated_at from the record we already have
                if (
                    record.updated_at is not None
                    and time.time() - record.updated_at <= self._idle_threshold
                ):
                    return False

            messages = await self._storage.load_messages(scope_key)
            if len(messages) <= self._keep_recent:
                return False

            pipeline_result = None
            if self._pipeline is not None:
                pipeline_result = await self._pipeline.run(
                    context=record.context,
                    messages=messages,
                    reason="idle_compact",
                    keep_recent_messages=self._keep_recent,
                )
                if pipeline_result.pruned_messages and not pipeline_result.archive_success:
                    # Archive was required but did not succeed (e.g. history_manager
                    # unavailable, or archive_strategy raised).  Do NOT truncate short-term to avoid data loss.
                    logger.warning(
                        "Auto-compact archive failed for %s; short-term left intact",
                        scope_key,
                    )
                    return False

                await self._storage.save_messages(
                    scope_key, pipeline_result.remaining_messages
                )
                summary = pipeline_result.summary or (
                    f"[Resumed Session] {len(pipeline_result.pruned_messages)} older messages were auto-compacted. "
                    f"Retained the most recent {len(pipeline_result.remaining_messages)} messages."
                )
                await self._storage.set(scope_key, ".auto_compact_summary", summary)
            else:
                # Fallback: simple tail-keep truncation (legacy path)
                kept = messages[-self._keep_recent :]
                pruned = messages[: -self._keep_recent]
                pruned_count = len(messages) - len(kept)

                if self._history_manager is not None and self._archive_strategy is not None:
                    from framework.memory.core.compression import CompressionResult

                    result = CompressionResult(
                        summary="",
                        metadata={"source": "auto_compact"},
                        pruned_messages=list(pruned),
                        remaining_messages=list(kept),
                    )
                    await self._archive_strategy.archive(
                        record.context,
                        list(pruned),
                        result,
                        self._history_manager,
                    )

                await self._storage.save_messages(scope_key, kept)
                summary = (
                    f"[Resumed Session] {pruned_count} older messages were auto-compacted. "
                    f"Retained the most recent {len(kept)} messages."
                )
                await self._storage.set(scope_key, ".auto_compact_summary", summary)

            # Update last_activity to compaction time to prevent immediate re-trigger
            await self._storage.set(scope_key, ".last_activity", time.time())

            if pipeline_result is not None:
                kept_count = len(pipeline_result.remaining_messages)
                pruned_count = len(pipeline_result.pruned_messages)
            else:
                kept_count = min(len(messages), self._keep_recent)
                pruned_count = max(0, len(messages) - self._keep_recent)

        logger.info(
            "Auto-compacted scope %s: kept %d, pruned %d",
            scope_key,
            kept_count,
            pruned_count,
        )
        return True
