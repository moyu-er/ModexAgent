"""DreamEngine — offline memory consolidation via KnowledgeConsolidator.

Uses a ReAct-based KnowledgeConsolidator agent to process archive entries
and update knowledge files.
"""

from __future__ import annotations

import asyncio
import logging

from modex_agent.agents.summarizer.abc import KnowledgeConsolidatorBase
from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.core.layers import ArchiveMemoryManager, KnowledgeMemoryManager
from modex_agent.memory.core.models import ArchiveEntry
from modex_agent.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
)
from modex_agent.memory.registry.base import MemoryStoreRegistry

logger = logging.getLogger(__name__)


class DreamEngine:
    """Offline DreamEngine: memory consolidation via KnowledgeConsolidator.

    Processes unprocessed archive entries through a ReAct-based
    KnowledgeConsolidator agent that reads archive files, inspects
    current knowledge, and produces targeted updates.

    Uses per-user locks so consolidation for user A does not block
    user B — each user's archive/knowledge storage is independent.
    """

    def __init__(
        self,
        history_manager: ArchiveMemoryManager,
        long_term_manager: KnowledgeMemoryManager,
        *,
        registry: MemoryStoreRegistry | None = None,
        consolidator: KnowledgeConsolidatorBase | None = None,
        max_consume_per_run: int = 3,
        per_archive_iterations: int = 10,
    ) -> None:
        self.history_manager = history_manager
        self.long_term_manager = long_term_manager
        self.registry = registry
        self.max_consume_per_run = max_consume_per_run
        self.per_archive_iterations = per_archive_iterations
        self._consolidator = consolidator
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, context: MemoryContext) -> asyncio.Lock:
        """Return the per-user lock for *context*.

        Archive storage is scoped by UserScope, so two different users
        have independent state.json files and can consolidate concurrently.
        """
        user_key = context.user_id or "default"
        lock = self._locks.get(user_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_key] = lock
        return lock

    async def run(self, context: MemoryContext) -> bool:
        lock = self._get_lock(context)
        if lock.locked():
            logger.info(
                "DreamEngine skipped: already running user=%s session=%s",
                context.user_id or "default",
                context.session_id,
            )
            return False

        async with lock:
            # Acquire invocation ID from archive state (self-incrementing counter,
            # same pattern as next_archive_id). Must be inside the lock to avoid
            # races on the counter.
            invocation_id = await self._next_invocation_id(context)
            unprocessed = await self.history_manager.get_unprocessed(
                context,
                cursor_name="dream",
                channel=ArchiveChannel.KNOWLEDGE,
            )
            entries = unprocessed.entries
            if not entries:
                logger.debug(
                    "DreamEngine: no unprocessed archives session=%s invocation=%d",
                    context.session_id,
                    invocation_id,
                )
                return False

            # Limit per run
            entries = entries[: self.max_consume_per_run]
            archive_ids = [e.entry_id for e in entries if e.entry_id]

            logger.info(
                "DreamEngine started: %d archive(s) ids=%s session=%s invocation=%d",
                len(archive_ids),
                archive_ids,
                context.session_id,
                invocation_id,
            )

            if self._consolidator is not None:
                result = await self._run_consolidator_limited(
                    entries,
                    context,
                    str(invocation_id),
                )
                logger.info(
                    "DreamEngine finished: success=%s session=%s invocation=%d",
                    result,
                    context.session_id,
                    invocation_id,
                )
                return result
            return False

    async def _run_consolidator_limited(
        self,
        entries: list[ArchiveEntry],
        context: MemoryContext,
        invocation_id: str = "",
    ) -> bool:
        """Run consolidator on a pre-sliced entry list."""
        assert self._consolidator is not None  # guarded by caller
        archive_ids = [e.entry_id for e in entries if e.entry_id]
        if not archive_ids:
            return False

        knowledge_dir = await self.long_term_manager.get_storage_path(context)
        if knowledge_dir is None:
            logger.warning(
                "DreamEngine: no knowledge storage path session=%s invocation=%s",
                context.session_id,
                invocation_id,
            )
            return False

        archive_base = await self.history_manager.get_storage_path(context)
        if archive_base is None:
            logger.warning(
                "DreamEngine: no archive storage path session=%s invocation=%s",
                context.session_id,
                invocation_id,
            )
            return False

        # Dynamic max_iterations: consolidator default + per-archive increment
        dynamic_iterations = (
            self._consolidator.max_iterations + len(archive_ids) * self.per_archive_iterations
        )

        logger.info(
            "DreamEngine consolidating: archive_ids=%s max_iterations=%d invocation=%s",
            archive_ids,
            dynamic_iterations,
            invocation_id,
        )

        success = await self._consolidator.consolidate(
            archive_ids=archive_ids,
            archive_base=archive_base,
            knowledge_dir=knowledge_dir,
            max_iterations=dynamic_iterations,
            invocation_id=invocation_id,
        )

        if success:
            final_cursor = max(archive_ids)
            await self._commit_knowledge_cursor(context, final_cursor)
            logger.info(
                "DreamEngine cursor advanced: knowledge_consumed_archive_id=%d invocation=%s",
                final_cursor,
                invocation_id,
            )

        return success

    async def _commit_knowledge_cursor(
        self,
        context: MemoryContext,
        cursor: int,
    ) -> None:
        await self.history_manager.commit_cursor(
            context,
            "dream",
            cursor,
            channel=ArchiveChannel.KNOWLEDGE,
        )
        await self.history_manager.prune_consumed_pairs(context)

    async def _next_invocation_id(self, context: MemoryContext) -> int:
        """Read and increment the knowledge invocation counter in archive state.

        Uses the same ``state.json`` as the archive layer — the counter is
        stored as ``knowledge_invocation_id`` alongside ``next_archive_id``
        and ``knowledge_consumed_archive_id``.

        Falls back to ``0`` when file-based storage is unavailable
        (e.g. in-memory backends for tests).
        """
        try:
            storage = await self.history_manager.get_storage_path(context)
        except Exception:
            return 0
        if storage is None:
            return 0
        try:
            from modex_agent.memory.stores.dir_archive import DirArchiveStorage

            dir_storage = DirArchiveStorage(storage)
            state = await dir_storage.read_archive_state() or {}
            current: int = state.get("knowledge_invocation_id", 0)
            next_id = current + 1
            state["knowledge_invocation_id"] = next_id
            await dir_storage.write_archive_state(state)
            return next_id
        except Exception:
            logger.debug("Failed to read/write invocation counter", exc_info=True)
            return 0

    async def scan_all(self) -> list[MemoryContext]:
        """Scan archive layer scope records, return processed MemoryContext list.

        Only processes main agent scopes; subagents are filtered out.
        Each scope calls run() to process unprocessed history entries.
        """
        processed: list[MemoryContext] = []
        if self.registry is None:
            logger.warning("DreamEngine.scan_all skipped: no registry configured")
            return processed

        records = await self.registry.list_records(
            layer=MemoryLayerName.ARCHIVE,
            agent_roles={MemoryAgentRole.MAIN},
        )
        for record in records:
            ctx = record.context
            if ctx is None:
                continue
            try:
                did_work = await self.run(ctx)
                if did_work:
                    processed.append(ctx)
            except Exception as e:
                logger.warning("DreamEngine failed for scope %s: %s", record.scope_key, e)
        return processed
