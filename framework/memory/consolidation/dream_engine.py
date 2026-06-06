"""DreamEngine — offline memory consolidation via KnowledgeConsolidator.

Uses a ReAct-based KnowledgeConsolidator agent to process archive entries
and update knowledge files.
"""

from __future__ import annotations

import asyncio
import logging

from framework.agents.summarizer.abc import KnowledgeConsolidatorBase
from framework.memory.archive_models import (
    KNOWLEDGE_ARCHIVE_FILE_KEY,
    ArchiveChannel,
)
from framework.memory.core.layers import ArchiveMemoryManager, KnowledgeMemoryManager
from framework.memory.core.models import ArchiveEntry
from framework.memory.core.scope import (
    MemoryAgentRole,
    MemoryContext,
    MemoryLayerName,
)
from framework.memory.registry.base import MemoryStoreRegistry

logger = logging.getLogger(__name__)


class DreamEngine:
    """Offline DreamEngine: memory consolidation via KnowledgeConsolidator.

    Processes unprocessed archive entries through a ReAct-based
    KnowledgeConsolidator agent that reads archive files, inspects
    current knowledge, and produces targeted updates.
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
        self._lock = asyncio.Lock()

    async def run(self, context: MemoryContext) -> bool:
        if self._lock.locked():
            logger.info("DreamEngine skipped: already running for session=%s", context.session_id)
            return False

        async with self._lock:
            unprocessed = await self.history_manager.get_unprocessed(
                context,
                cursor_name="dream",
                channel=ArchiveChannel.KNOWLEDGE,
            )
            entries = unprocessed.entries
            if not entries:
                return False

            # Limit per run
            entries = entries[:self.max_consume_per_run]

            # NEW PATH: Use KnowledgeConsolidator agent
            if self._consolidator is not None:
                return await self._run_consolidator_limited(entries, context)
            return False

    async def _run_consolidator_limited(
        self,
        entries: list[ArchiveEntry],
        context: MemoryContext,
    ) -> bool:
        """Run consolidator on a pre-sliced entry list."""
        assert self._consolidator is not None  # guarded by caller
        archive_ids = [e.entry_id for e in entries if e.entry_id]
        if not archive_ids:
            return False

        knowledge_dir = await self.long_term_manager.get_storage_path(context)
        if knowledge_dir is None:
            logger.warning(
                "KnowledgeConsolidator: no knowledge storage path for context=%s",
                context,
            )
            return False

        archive_base = await self.history_manager.get_storage_path(context)
        if archive_base is None:
            logger.warning(
                "KnowledgeConsolidator: no archive storage path for context=%s",
                context,
            )
            return False

        # Dynamic max_iterations: consolidator default + per-archive increment
        dynamic_iterations = (
            self._consolidator.max_iterations
            + len(archive_ids) * self.per_archive_iterations
        )

        logger.info(
            "KnowledgeConsolidator: processing %d archive(s) for knowledge update, max_iterations=%d",
            len(archive_ids), dynamic_iterations,
        )

        success = await self._consolidator.consolidate(
            archive_ids=archive_ids,
            archive_base=archive_base,
            knowledge_dir=knowledge_dir,
            max_iterations=dynamic_iterations,
        )

        final_cursor = max(archive_ids)
        await self._commit_knowledge_cursor(context, final_cursor)

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
            has_file=KNOWLEDGE_ARCHIVE_FILE_KEY,
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
