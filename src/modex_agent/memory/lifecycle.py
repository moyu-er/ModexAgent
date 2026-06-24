"""Lifecycle and maintenance policy ABCs and default implementations.

Phase 6 — turn/session lifecycle hooks and background maintenance tasks.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from modex_agent.memory.archive_models import ArchiveChannel
from modex_agent.memory.core.layers import MemoryLayerSet
from modex_agent.memory.core.scope import (
    MemoryContext,
    MemoryLayerName,
)
from modex_agent.memory.registry.base import MemoryStoreRegistry
from modex_agent.memory.stores.dir_archive import DirArchiveStorage

logger = logging.getLogger(__name__)


# ── Maintenance ─────────────────────────────────────────────────────────────


@dataclass
class MaintenanceResult:
    scope_key: str
    task: str
    success: bool
    detail: str | None = None


class DefaultMemoryMaintenancePolicy:
    """Default maintenance: idle auto-compact, archive/knowledge retention enforcement."""

    def __init__(
        self,
        archive_retention_policy: ArchiveRetentionPolicy | None = None,
        knowledge_retention_policy: KnowledgeRetentionPolicy | None = None,
    ) -> None:
        self._archive_retention = archive_retention_policy
        self._knowledge_retention = knowledge_retention_policy

    async def scan_once(
        self,
        *,
        registry: MemoryStoreRegistry,
        layers: MemoryLayerSet,
    ) -> list[MaintenanceResult]:
        import time

        results: list[MaintenanceResult] = []
        has_work = self._archive_retention is not None or self._knowledge_retention is not None
        if not has_work:
            return results

        # ── Archive retention enforcement ─────────────────────────────────────
        if self._archive_retention is not None and layers.archive is not None:
            try:
                archive_records = await registry.list_records(layer=MemoryLayerName.ARCHIVE)
            except Exception:
                logger.warning("Maintenance scan failed to list archive records", exc_info=True)
                archive_records = []

            for record in archive_records:
                ctx = record.context
                if ctx is None:
                    continue
                try:
                    archive_storage = await registry.resolve(
                        layer=MemoryLayerName.ARCHIVE,
                        scope=layers.archive.get_scope(),
                        context=ctx,
                    )
                    entries = await archive_storage.read_channel_logs(
                        ArchiveChannel.CONTEXT.value,
                        since_archive_id=0,
                        limit=1_000_000,
                    )
                    if not entries:
                        continue

                    max_entries = await self._archive_retention.get_max_entries(ctx)
                    max_age_days = await self._archive_retention.get_max_age_days(ctx)
                    pruned = False

                    if max_entries is not None and len(entries) > max_entries:
                        kept = entries[-max_entries:]
                        kept_ids = {int(e.get("archive_id", e.get("cursor", 0)) or 0) for e in kept}
                        await archive_storage.save_channel_logs(ArchiveChannel.CONTEXT.value, kept)
                        # Also prune KNOWLEDGE channel to match retained CONTEXT entries
                        knowledge_entries = await archive_storage.read_channel_logs(
                            ArchiveChannel.KNOWLEDGE.value,
                            since_archive_id=0,
                            limit=1_000_000,
                        )
                        knowledge_kept = [
                            e
                            for e in knowledge_entries
                            if int(e.get("archive_id", 0) or 0) in kept_ids
                        ]
                        await archive_storage.save_channel_logs(
                            ArchiveChannel.KNOWLEDGE.value,
                            knowledge_kept,
                        )
                        entries = kept
                        pruned = True

                    if max_age_days is not None:
                        cutoff = time.time() - (max_age_days * 86400)
                        kept = []
                        for entry in entries:
                            created_at = entry.get("created_at")
                            entry_time: float | None = None
                            if isinstance(created_at, str):
                                from datetime import datetime

                                entry_time = datetime.fromisoformat(created_at).timestamp()
                            elif isinstance(created_at, int | float):
                                entry_time = float(created_at)
                            if entry_time is not None and entry_time < cutoff:
                                pruned = True
                                continue
                            kept.append(entry)
                        if pruned and len(kept) != len(entries):
                            kept_ids = {
                                int(e.get("archive_id", e.get("cursor", 0)) or 0) for e in kept
                            }
                            await archive_storage.save_channel_logs(
                                ArchiveChannel.CONTEXT.value, kept
                            )
                            knowledge_entries = await archive_storage.read_channel_logs(
                                ArchiveChannel.KNOWLEDGE.value,
                                since_archive_id=0,
                                limit=1_000_000,
                            )
                            knowledge_kept = [
                                e
                                for e in knowledge_entries
                                if int(e.get("archive_id", 0) or 0) in kept_ids
                            ]
                            await archive_storage.save_channel_logs(
                                ArchiveChannel.KNOWLEDGE.value,
                                knowledge_kept,
                            )

                    # FIFO eviction: delete oldest dirs exceeding max_archive_total,
                    # but never delete unconsumed archives.
                    max_total = await self._archive_retention.get_max_archive_total(ctx)
                    if max_total is not None:
                        # DirArchiveStorage manages archive directories directly.
                        # When registry returns a different storage type, look up
                        # the archive directory via the layer manager and wrap it.
                        dir_storage = (
                            archive_storage
                            if isinstance(archive_storage, DirArchiveStorage)
                            else None
                        )
                        if dir_storage is None and layers.archive is not None:
                            try:
                                archive_dir = await layers.archive.get_storage_path(ctx)
                                if archive_dir is not None:
                                    dir_storage = DirArchiveStorage(archive_dir)
                            except Exception:
                                pass
                        if dir_storage is not None:
                            state = await dir_storage.read_archive_state() or {}
                            consumed = state.get("knowledge_consumed_archive_id", 0)
                            deleted = await dir_storage.prune_to_max(
                                max_total, min_safe_id=consumed
                            )
                            if deleted:
                                await dir_storage.cleanup_empty_dirs()
                                pruned = True

                    if pruned:
                        results.append(
                            MaintenanceResult(
                                scope_key=record.scope_key,
                                task="archive_retention",
                                success=True,
                            )
                        )
                except Exception as exc:
                    logger.warning("Archive retention failed for %s: %s", record.scope_key, exc)
                    results.append(
                        MaintenanceResult(
                            scope_key=record.scope_key,
                            task="archive_retention",
                            success=False,
                            detail=str(exc),
                        )
                    )

        # ── Knowledge eviction ────────────────────────────────────────────────
        if self._knowledge_retention is not None and layers.knowledge is not None:
            try:
                knowledge_records = await registry.list_records(layer=MemoryLayerName.KNOWLEDGE)
            except Exception:
                logger.warning("Maintenance scan failed to list knowledge records", exc_info=True)
                knowledge_records = []

            for record in knowledge_records:
                ctx = record.context
                if ctx is None:
                    continue
                try:
                    knowledge_storage = await registry.resolve(
                        layer=MemoryLayerName.KNOWLEDGE,
                        scope=layers.knowledge.get_scope(),
                        context=ctx,
                    )
                    keys = await knowledge_storage.list_keys()
                    keys = [k for k in keys if not k.endswith("._meta")]
                    if not keys:
                        continue

                    # Build file -> last-update map from changelog
                    changelog = await knowledge_storage.read_logs(since_cursor=0)
                    file_last_update: dict[str, float] = {}
                    for entry in changelog:
                        file_name = entry.get("file")
                        created_at = entry.get("created_at")
                        if not file_name or not created_at:
                            continue
                        knowledge_entry_time: float | None = None
                        if isinstance(created_at, str):
                            from datetime import datetime

                            knowledge_entry_time = datetime.fromisoformat(created_at).timestamp()
                        elif isinstance(created_at, int | float):
                            knowledge_entry_time = float(created_at)
                        if knowledge_entry_time is not None:
                            prev = file_last_update.get(file_name, 0.0)
                            file_last_update[file_name] = max(prev, knowledge_entry_time)

                    pruned = False
                    for key in keys:
                        if self._knowledge_retention.is_permanent_file(key):
                            continue
                        stale_days = self._knowledge_retention.get_stale_threshold_days(key)
                        if stale_days is None:
                            continue
                        last_update = file_last_update.get(key, record.updated_at or 0.0)
                        if time.time() - last_update > stale_days * 86400:
                            await knowledge_storage.delete(key)
                            pruned = True

                    if pruned:
                        results.append(
                            MaintenanceResult(
                                scope_key=record.scope_key,
                                task="knowledge_eviction",
                                success=True,
                            )
                        )
                except Exception as exc:
                    logger.warning("Knowledge eviction failed for %s: %s", record.scope_key, exc)
                    results.append(
                        MaintenanceResult(
                            scope_key=record.scope_key,
                            task="knowledge_eviction",
                            success=False,
                            detail=str(exc),
                        )
                    )

        return results


# ── Retention ───────────────────────────────────────────────────────────────


class ArchiveRetentionPolicy(ABC):
    """Archive layer aging: max entries, max age, max total."""

    @abstractmethod
    async def get_max_entries(self, context: MemoryContext) -> int | None: ...

    @abstractmethod
    async def get_max_age_days(self, context: MemoryContext) -> int | None: ...

    async def get_max_archive_total(self, context: MemoryContext) -> int | None:
        """Default: no total limit. Override to enable FIFO eviction."""
        return None


class DefaultArchiveRetentionPolicy(ArchiveRetentionPolicy):
    def __init__(
        self,
        max_entries: int | None = 1000,
        max_age_days: int | None = None,
        max_archive_total: int | None = None,
    ) -> None:
        self._max_entries = max_entries
        self._max_age_days = max_age_days
        self._max_archive_total = max_archive_total

    async def get_max_entries(self, context: MemoryContext) -> int | None:
        _ = context
        return self._max_entries

    async def get_max_age_days(self, context: MemoryContext) -> int | None:
        _ = context
        return self._max_age_days

    async def get_max_archive_total(self, context: MemoryContext) -> int | None:
        return self._max_archive_total


class KnowledgeRetentionPolicy(ABC):
    """Knowledge layer aging: which files are permanent, stale thresholds."""

    @abstractmethod
    def is_permanent_file(self, file_key: str) -> bool: ...

    @abstractmethod
    def get_stale_threshold_days(self, file_key: str) -> int | None: ...


class DefaultKnowledgeRetentionPolicy(KnowledgeRetentionPolicy):
    def __init__(
        self,
        stale_days: int = 14,
        default_files: dict[str, str] | None = None,
    ) -> None:
        self._stale_days = stale_days
        self._default_files = default_files or {
            "soul": "SOUL.md",
            "user": "USER.md",
            "memory": "MEMORY.md",
        }
        self._protected_logical = {"soul", "user"}

    def is_permanent_file(self, file_key: str) -> bool:
        # Check logical key
        if file_key in self._protected_logical:
            return True
        # Check storage key mapped from a protected logical key
        for logical, storage in self._default_files.items():
            if logical in self._protected_logical and file_key == storage:
                return True
        return False

    def get_stale_threshold_days(self, file_key: str) -> int | None:
        memory_file = self._default_files.get("memory", "MEMORY.md")
        if file_key in ("memory", memory_file):
            return self._stale_days
        return None
