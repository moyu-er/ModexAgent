"""Default archive memory manager backed by scoped storage factories."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from framework.memory.core.layers import ArchiveMemoryManager
from framework.memory.core.models import ArchiveEntry, UnprocessedResult
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.history_search import (
    HistorySearchStrategy,
    RecentFirstHistorySearch,
)
from framework.memory.layers.config import ArchiveMemoryConfig, StorageFactory


class ScopedArchiveMemoryManager(ArchiveMemoryManager):
    """Archive layer manager that resolves storage through a StorageFactory."""

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: ArchiveMemoryConfig | None = None,
        search_strategy: HistorySearchStrategy | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or ArchiveMemoryConfig()
        self._search_strategy = search_strategy or RecentFirstHistorySearch()

    def get_scope(self) -> MemoryScope:
        return self._config.scope

    async def append(self, context: MemoryContext, entry: ArchiveEntry) -> ArchiveEntry:
        storage = await self._storage_factory(context)
        payload = {
            "summary": entry.summary,
            "metadata": dict(entry.metadata),
            "raw_refs": list(entry.raw_refs),
            "session_id": context.session_id,
        }
        if entry.created_at is not None:
            payload["created_at"] = entry.created_at.isoformat()
        stored = await storage.append_log(payload)
        await self._maybe_prune(context)
        created_at = stored.get("created_at")
        return ArchiveEntry(
            summary=str(stored.get("summary", "")),
            metadata=stored.get("metadata") or {},
            entry_id=int(stored["entry_id"]) if stored.get("entry_id") is not None else None,
            created_at=datetime.fromisoformat(created_at) if isinstance(created_at, str) else None,
            raw_refs=list(stored.get("raw_refs") or []),
        )

    async def _maybe_prune(self, context: MemoryContext) -> None:
        if self._config.max_entries is None:
            return
        storage = await self._storage_factory(context)
        entries = await storage.read_logs(since_cursor=0)
        if len(entries) <= self._config.max_entries:
            return
        await storage.save_logs(entries[-self._config.max_entries :])

    async def get_recent(self, context: MemoryContext, limit: int = 5) -> list[ArchiveEntry]:
        storage = await self._storage_factory(context)
        entries = await storage.read_logs(since_cursor=0)
        return [self._entry_from_dict(entry) for entry in (entries[-limit:] if limit else entries)]

    async def search(
        self,
        context: MemoryContext,
        query: str,
        limit: int = 5,
    ) -> list[ArchiveEntry]:
        storage = await self._storage_factory(context)
        entries = await storage.read_logs(since_cursor=0)
        results = await self._search_strategy.search(entries, query, limit)
        return [self._entry_from_dict(entry) for entry in results]

    async def get_unprocessed(
        self,
        context: MemoryContext,
        cursor_name: str,
        limit: int = 100,
    ) -> UnprocessedResult:
        storage = await self._storage_factory(context)
        since = await storage.get_last_cursor(cursor_name)
        entries = await storage.read_logs(since_cursor=since, limit=limit)
        cursor = max((entry.get("cursor", 0) for entry in entries), default=since)
        return UnprocessedResult(
            cursor=cursor,
            entries=[self._entry_from_dict(entry) for entry in entries],
        )

    async def commit_cursor(
        self,
        context: MemoryContext,
        cursor_name: str,
        cursor: int,
    ) -> None:
        storage = await self._storage_factory(context)
        await storage.set_last_cursor(cursor_name, cursor)

    async def clear(self, context: MemoryContext) -> None:
        storage = await self._storage_factory(context)
        await storage.save_logs([])

    def _entry_from_dict(self, entry: dict[str, object]) -> ArchiveEntry:
        created_at = entry.get("created_at")
        if isinstance(created_at, str):
            parsed_created_at = datetime.fromisoformat(created_at)
        else:
            parsed_created_at = None
        metadata = entry.get("metadata")
        raw_refs = entry.get("raw_refs")
        raw_entry_id = entry.get("entry_id")
        entry_id = int(raw_entry_id) if isinstance(raw_entry_id, int | str) else None
        return ArchiveEntry(
            summary=str(entry.get("summary") or ""),
            metadata=metadata if isinstance(metadata, Mapping) else {},
            entry_id=entry_id,
            created_at=parsed_created_at,
            raw_refs=list(raw_refs) if isinstance(raw_refs, list) else [],
        )
