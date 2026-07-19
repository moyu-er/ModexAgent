"""Default archive memory manager backed by scoped storage factories."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modex_agent.core.scope import MemoryContext, Scope
from modex_agent.memory.archive_models import (
    ArchiveBundleResult,
    ArchiveChannel,
    ArchiveGenerationResult,
    ArchiveState,
    ArchiveWrite,
)
from modex_agent.memory.core.layers import ArchiveMemoryManager
from modex_agent.memory.core.lock import StorageLock
from modex_agent.memory.core.models import ArchiveEntry, UnprocessedResult
from modex_agent.memory.core.split_stores import ArchiveStore, MemoryStoreBundle
from modex_agent.memory.core.store_metadata import StoreMetadata
from modex_agent.memory.history_search import (
    HistorySearchStrategy,
    RecentFirstHistorySearch,
)
from modex_agent.memory.layers.config import ArchiveMemoryConfig, StorageFactory

logger = logging.getLogger(__name__)


def _get_bundle_lock(bundle: MemoryStoreBundle) -> StorageLock | None:
    """Return the storage lock from the bundle's concrete store, or None."""
    store = bundle.messages
    if isinstance(store, StoreMetadata):
        return store.get_lock()
    return None


def _require_archive(bundle: MemoryStoreBundle) -> ArchiveStore:
    """Return the archive store from the bundle, asserting it is present."""
    assert bundle.archive is not None, "Archive layer requires bundle.archive"
    return bundle.archive


def _parse_created_at(value: object) -> datetime | None:
    """Parse a ``created_at`` value into a timezone-aware datetime.

    The file backend serialises timestamps as int ms (T9); legacy data and
    the SQLite adapter may also emit ISO-8601 strings or float ms. Returns
    ``None`` for missing/empty values.
    """
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000.0, tz=UTC)
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


class ScopedArchiveMemoryManager(ArchiveMemoryManager):
    """Archive layer manager that resolves storage through a StorageFactory.

    Coordinate model
    ---------------
    ``archive_id`` is the **single** monotonic coordinate shared by both
    CONTEXT and KNOWLEDGE channels.  Entry ``cursor`` is set equal to
    ``archive_id`` at write time.  ``.archive_state.json`` is the sole
    source of truth::

        {"next_archive_id": N, "knowledge_consumed_archive_id": K}

    CONTEXT injection reads ALL entries unconditionally
    (``since_archive_id=0``) — it does **not** depend on
    ``knowledge_consumed_archive_id``.

    KNOWLEDGE is consumed by DreamEngine via
    ``get_unprocessed(channel=KNOWLEDGE)`` which reads only entries with
    ``archive_id > knowledge_consumed_archive_id``.  After Dream commits
    the cursor, ``prune_consumed_pairs()`` removes entries from **both**
    channels whose ``archive_id ≤ knowledge_consumed_archive_id −
    retained_consumed_archive_pairs`` (default retention: 3 consumed pairs).

    This means consumed KNOWLEDGE entries do NOT immediately remove the
    corresponding CONTEXT entries from injection — they persist until the
    retention window advances past them.
    """

    def __init__(
        self,
        storage_factory: StorageFactory,
        config: ArchiveMemoryConfig | None = None,
        search_strategy: HistorySearchStrategy | None = None,
    ) -> None:
        self._storage_factory = storage_factory
        self._config = config or ArchiveMemoryConfig()
        self._search_strategy = search_strategy or RecentFirstHistorySearch()

    def get_scope(self) -> Scope:
        return self._config.scope

    async def get_storage_path(self, context: MemoryContext) -> Path | None:
        """Resolve the scoped archive storage directory for *context*."""
        try:
            bundle = await self._storage_factory(context)
            if isinstance(bundle.archive, StoreMetadata):
                return bundle.archive.base_path
            return None
        except Exception:
            logger.warning("Failed to resolve archive directory", exc_info=True)
            return None

    async def _do_prune(self, archive: ArchiveStore) -> None:
        state = await self._load_state(archive)
        safe_delete = (
            state.knowledge_consumed_archive_id - self._config.retained_consumed_archive_pairs
        )
        if safe_delete <= 0:
            return
        for channel in (ArchiveChannel.CONTEXT, ArchiveChannel.KNOWLEDGE):
            entries = await self._read_channel_logs(archive, channel)
            kept = [entry for entry in entries if self._archive_id(entry) > safe_delete]
            if len(kept) != len(entries):
                await self._save_channel_logs(archive, channel, kept)

    async def append(self, context: MemoryContext, entry: ArchiveEntry) -> ArchiveEntry:
        metadata = dict(entry.metadata)
        if entry.created_at is not None:
            metadata["created_at"] = int(entry.created_at.timestamp() * 1000)
        result = await self.append_bundle(
            context,
            (
                ArchiveWrite(
                    channel=ArchiveChannel.CONTEXT,
                    summary=entry.summary,
                    metadata=metadata,
                    raw_refs=tuple(entry.raw_refs),
                ),
            ),
        )
        entries = await self.get_recent(context, limit=1, channel=ArchiveChannel.CONTEXT)
        stored_entry = entries[-1]
        return ArchiveEntry(
            summary=stored_entry.summary,
            metadata=stored_entry.metadata,
            entry_id=result.archive_id,
            created_at=stored_entry.created_at,
            raw_refs=stored_entry.raw_refs,
        )

    async def append_bundle(
        self,
        context: MemoryContext,
        writes: Sequence[ArchiveWrite],
    ) -> ArchiveBundleResult:
        if not writes:
            return ArchiveBundleResult(archive_id=0, written_channels=())
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        written: list[ArchiveChannel] = []
        lock = _get_bundle_lock(bundle)
        archive_id_box: list[int] = [0]

        async def _do_append() -> None:
            state = await self._load_state(archive)
            archive_id_box[0] = state.next_archive_id
            for write in writes:
                payload = self._payload_for_write(context, write, state.next_archive_id)
                await self._append_channel_log(archive, write.channel, payload)
                written.append(write.channel)
            await self._save_state(
                archive,
                ArchiveState(
                    next_archive_id=state.next_archive_id + 1,
                    knowledge_consumed_archive_id=state.knowledge_consumed_archive_id,
                ),
            )
            await self._do_prune(archive)

        if lock is not None:
            async with lock.write():
                await _do_append()
        else:
            await _do_append()
        return ArchiveBundleResult(archive_id=archive_id_box[0], written_channels=tuple(written))

    async def append_generation(
        self,
        context: MemoryContext,
        generation: ArchiveGenerationResult,
    ) -> ArchiveBundleResult:
        result = await self.append_bundle(context, generation.writes)
        bundle = await self._storage_factory(context)
        if isinstance(bundle.messages, StoreMetadata) and bundle.messages.base_path is not None:
            archive_dir = bundle.messages.base_path / str(result.archive_id)
            archive_dir.mkdir(parents=True, exist_ok=True)
            (archive_dir / "context.md").write_text(generation.documents.context, encoding="utf-8")
            (archive_dir / "knowledge.md").write_text(
                generation.documents.knowledge,
                encoding="utf-8",
            )
            (archive_dir / "index.md").write_text(generation.documents.index, encoding="utf-8")
        return result

    async def _load_state(self, archive: ArchiveStore) -> ArchiveState:
        raw = await archive.read_archive_state()
        if isinstance(raw, Mapping):
            return ArchiveState(
                next_archive_id=int(raw.get("next_archive_id", 1)),
                knowledge_consumed_archive_id=int(raw.get("knowledge_consumed_archive_id", 0)),
            )
        return ArchiveState()

    async def _save_state(self, archive: ArchiveStore, state: ArchiveState) -> None:
        payload = {
            "next_archive_id": state.next_archive_id,
            "knowledge_consumed_archive_id": state.knowledge_consumed_archive_id,
        }
        await archive.write_archive_state(payload)

    async def _append_channel_log(
        self,
        archive: ArchiveStore,
        channel: ArchiveChannel,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        return await archive.append_channel_log(channel.value, payload)

    async def _read_channel_logs(
        self,
        archive: ArchiveStore,
        channel: ArchiveChannel,
        *,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, object]]:
        return await archive.read_channel_logs(channel.value, since_archive_id, limit)

    async def _save_channel_logs(
        self,
        archive: ArchiveStore,
        channel: ArchiveChannel,
        entries: list[dict[str, object]],
    ) -> None:
        await archive.save_channel_logs(channel.value, entries)

    def _payload_for_write(
        self,
        context: MemoryContext,
        write: ArchiveWrite,
        archive_id: int,
    ) -> dict[str, object]:
        metadata = {
            **dict(write.metadata),
            "archive_id": archive_id,
            "source_session_id": context.session_id,
            "source_agent_id": context.agent_id,
            "source_agent_role": str(context.agent_role)
            if context.agent_role is not None
            else None,
        }
        created_at = metadata.get("created_at")
        return {
            "archive_id": archive_id,
            "entry_id": archive_id,
            "channel": write.channel.value,
            "summary": write.summary,
            "metadata": metadata,
            "raw_refs": list(write.raw_refs),
            "session_id": context.session_id,
            "created_at": created_at if isinstance(created_at, str) else None,
        }

    @staticmethod
    def _filter_channel(
        entries: list[dict[str, object]],
        channel: ArchiveChannel,
    ) -> list[dict[str, object]]:
        return [entry for entry in entries if entry.get("channel") == channel.value]

    @staticmethod
    def _archive_id(entry: Mapping[str, object]) -> int:
        raw = entry.get("archive_id", 0)
        return int(raw) if isinstance(raw, int | str) else 0

    async def _append_raw(self, context: MemoryContext, entry: ArchiveEntry) -> ArchiveEntry:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        stored = await archive.append_log(
            {
                "summary": entry.summary,
                "metadata": dict(entry.metadata),
                "raw_refs": list(entry.raw_refs),
                "session_id": context.session_id,
                "channel": ArchiveChannel.CONTEXT.value,
                "created_at": int(entry.created_at.timestamp() * 1000)
                if entry.created_at
                else None,
            }
        )
        await self._maybe_prune(context)
        created_at = stored.get("created_at")
        return ArchiveEntry(
            summary=str(stored.get("summary", "")),
            metadata=stored.get("metadata") or {},
            entry_id=int(stored["entry_id"]) if stored.get("entry_id") is not None else None,
            created_at=_parse_created_at(created_at),
            raw_refs=list(stored.get("raw_refs") or []),
        )

    async def _maybe_prune(self, context: MemoryContext) -> None:
        if self._config.max_entries is None:
            return
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        entries = await archive.read_logs(since_cursor=0)
        if len(entries) <= self._config.max_entries:
            return
        await archive.save_logs(entries[-self._config.max_entries :])

    async def get_recent(
        self,
        context: MemoryContext,
        limit: int = 5,
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[ArchiveEntry]:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        channel_entries = await self._read_channel_logs(archive, channel)
        selected = channel_entries[-limit:] if limit else channel_entries
        return [self._entry_from_dict(entry) for entry in selected]

    async def search(
        self,
        context: MemoryContext,
        query: str,
        limit: int = 5,
        *,
        channel: ArchiveChannel = ArchiveChannel.CONTEXT,
    ) -> list[ArchiveEntry]:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        entries = await self._read_channel_logs(archive, channel)
        results = await self._search_strategy.search(entries, query, limit)
        return [self._entry_from_dict(entry) for entry in results]

    async def get_unprocessed(
        self,
        context: MemoryContext,
        cursor_name: str,
        limit: int = 100,
        *,
        channel: ArchiveChannel = ArchiveChannel.KNOWLEDGE,
    ) -> UnprocessedResult:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        state = await self._load_state(archive)
        since = state.knowledge_consumed_archive_id
        channel_entries = await self._read_channel_logs(
            archive,
            channel,
            since_archive_id=since,
        )
        selected = channel_entries[:limit] if limit else channel_entries
        cursor = max((self._archive_id(entry) for entry in selected), default=since)
        return UnprocessedResult(
            cursor=cursor,
            entries=[self._entry_from_dict(entry) for entry in selected],
        )

    async def commit_cursor(
        self,
        context: MemoryContext,
        cursor_name: str,
        cursor: int,
        *,
        channel: ArchiveChannel = ArchiveChannel.KNOWLEDGE,
    ) -> None:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        lock = _get_bundle_lock(bundle)

        async def _do_commit() -> None:
            state = await self._load_state(archive)
            await self._save_state(
                archive,
                ArchiveState(
                    next_archive_id=state.next_archive_id,
                    knowledge_consumed_archive_id=cursor,
                ),
            )

        if lock is not None:
            async with lock.write():
                await _do_commit()
        else:
            await _do_commit()

    async def prune_consumed_pairs(self, context: MemoryContext) -> None:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        lock = _get_bundle_lock(bundle)
        if lock is not None:
            async with lock.write():
                await self._do_prune(archive)
        else:
            await self._do_prune(archive)

    async def clear(self, context: MemoryContext) -> None:
        bundle = await self._storage_factory(context)
        archive = _require_archive(bundle)
        await self._save_channel_logs(archive, ArchiveChannel.CONTEXT, [])
        await self._save_channel_logs(archive, ArchiveChannel.KNOWLEDGE, [])

    def _entry_from_dict(self, entry: dict[str, object]) -> ArchiveEntry:
        created_at = entry.get("created_at")
        parsed_created_at = _parse_created_at(created_at)
        metadata = entry.get("metadata")
        raw_refs = entry.get("raw_refs")
        raw_entry_id = entry.get("archive_id", entry.get("entry_id"))
        entry_id = int(raw_entry_id) if isinstance(raw_entry_id, int | str) else None
        metadata_dict = dict(metadata) if isinstance(metadata, Mapping) else {}
        if "archive_id" not in metadata_dict and entry_id is not None:
            metadata_dict["archive_id"] = entry_id
        return ArchiveEntry(
            summary=str(entry.get("summary") or ""),
            metadata=metadata_dict,
            entry_id=entry_id,
            created_at=parsed_created_at,
            raw_refs=list(raw_refs) if isinstance(raw_refs, list) else [],
        )
