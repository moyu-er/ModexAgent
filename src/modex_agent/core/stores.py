"""Shared storage contracts — split store ABCs, revisions, and store ports.

The storage seam every backend speaks (W3b): memory implementations and
persistence adapters BOTH import these ABCs from core, so neither the memory
package nor the persistence package owns the contract the other implements.

Four focused, deep ABCs, each owning one storage concern:

- :class:`MessageStore`  — conversation message history (9 methods)
- :class:`KVStore`       — scoped key/value records (4 methods)
- :class:`CursorStore`   — monotonic processing cursors (2 methods)
- :class:`ArchiveStore`  — append-only archive logs + channel logs (10 methods)

These four are composed by :class:`MemoryStoreBundle`, a frozen Pydantic model
holding the three required stores (``messages`` / ``kv`` / ``cursors``) and an
optional ``archive`` (sessions without archival history pass ``archive=None``).

Two additional store contracts live here because they are implemented on both
sides of the memory/persistence seam:

- :class:`StorageRevision` — revision metadata returned by scoped writes.
- :class:`PoolRoutingStore` — session-prefix → pool routing persistence.
- :class:`ScopedBundleFactory` — the port the memory registry's hybrid
  implementation uses to obtain structured (DB-backed) bundles from a
  persistence manager without importing persistence.

All methods are async: the existing backends use async locks (see
``modex_agent.memory.core.lock``) and async file I/O.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.scope import RecordScope

__all__ = [
    "ArchiveStore",
    "CursorStore",
    "KVStore",
    "MemoryStoreBundle",
    "MessageStore",
    "PoolRoutingStore",
    "ScopedBundleFactory",
    "StorageRevision",
    "message_signature",
]

# Fields stripped before comparing two message dicts for identity.
# _pinned / _deleted are runtime markers added by load_messages/load_all_messages.
# reasoning_content is persisted by ChatMessage.to_dict() but excluded from
# identity matching: rows stored before the passback change lack the field,
# so signatures must stay insensitive to it. content_format is omitted by
# to_dict() when PLAIN.
_META_FIELDS: frozenset[str] = frozenset(
    {
        "_pinned",
        "_deleted",
        "reasoning_content",
        "content_format",
        "token_count",
        "created_at",
    }
)


def message_signature(msg: dict[str, Any]) -> str:
    """Canonical JSON signature for message identity matching.

    Strips runtime markers and metadata fields that may differ between
    the stored form and the ``ChatMessage.to_dict()`` round-trip, then
    serialises with sorted keys so dict key-order never causes a mismatch.
    """
    m = {k: v for k, v in msg.items() if k not in _META_FIELDS and v is not None}
    return json.dumps(m, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class StorageRevision:
    """Revision metadata returned by scoped storage writes.

    ``updated_at`` is a Unix-epoch millisecond integer (ADR-0029 §6). Both
    file and SQLite backends pass ``now_ms()`` directly — no ``datetime``
    bridge at the adapter boundary.
    """

    message_count: int
    updated_at: int
    version: int = 0


class MessageStore(ABC):
    """Conversation message history for one scoped memory layer.

    Owns the short-term message list: load/save/append, revision tracking,
    pinning, pruning, deletion, and expired-message cleanup.

    **Soft-delete model.**  ``prune_messages`` soft-deletes (marks as
    deleted) rather than physically removing rows.  ``retain_messages``
    replaces the active set with the given list: pruned rows are
    soft-deleted, stale copies of kept rows are marked ``superseded``.
    ``load_messages`` returns only active messages; ``load_all_messages``
    returns including soft-deleted ones (used by context fork) but never
    superseded copies.  Physical removal happens via ``cleanup_expired``
    (TTL, both deleted states) or ``delete_session_rows`` (session deletion).
    """

    @abstractmethod
    async def load_messages(self) -> list[dict[str, Any]]:
        """Return active messages (excludes soft-deleted)."""
        ...

    @abstractmethod
    async def load_all_messages(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return all messages including soft-deleted ones, excluding COMPACT.

        Soft-deleted messages carry a ``_deleted: True`` marker.
        COMPACT role messages are always excluded (internal compaction
        bookkeeping, not real conversational history). Used by context fork
        to access full parent history. When *limit* is provided, return the
        most recent messages in chronological order.
        """
        ...

    @abstractmethod
    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        """Atomically replace the message list; return the new revision.

        This is a hard replace — all existing rows (including soft-deleted)
        are removed and only *messages* are stored.  Use
        :meth:`retain_messages` for the soft-delete cleanup path.
        """
        ...

    @abstractmethod
    async def append_message(self, message: dict[str, Any]) -> StorageRevision:
        """Append a single message; return the new revision."""
        ...

    @abstractmethod
    async def get_revision(self) -> StorageRevision:
        """Return the current revision (message count + version + timestamp)."""
        ...

    @abstractmethod
    async def prune_messages(self, max_messages: int) -> tuple[int, list[dict[str, Any]]]:
        """Trim history to ``max_messages`` newest; return ``(pruned_count, pruned)``."""
        ...

    @abstractmethod
    async def pin_message(self, message_id: str) -> None:
        """Mark a message as pinned so it survives pruning."""
        ...

    @abstractmethod
    async def unpin_message(self, message_id: str) -> None:
        """Remove the pin from a previously pinned message."""
        ...

    @abstractmethod
    async def delete_message(self, message_id: str) -> bool:
        """Delete a single message by id; return whether it existed."""
        ...

    @abstractmethod
    async def cleanup_expired(self) -> int:
        """Remove TTL-expired messages; return the count removed."""
        ...

    @abstractmethod
    async def retain_messages(
        self,
        keep_messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        """Replace the active set with exactly *keep_messages*, in order.

        After the call, ``load_messages()`` returns *keep_messages* in the
        given order — including entries that did not previously exist (e.g. a
        compact summary prepended by session cleanup).  Removed messages are
        not physically deleted:

        - messages absent from *keep_messages* are soft-deleted (visible via
          :meth:`load_all_messages`, purged by TTL);
        - prior physical copies of kept messages are marked ``superseded``
          (invisible to every read path, purged by TTL).

        When *expected_revision* is provided and does not match the current
        revision, returns ``None`` without modifying anything.
        """
        ...

    @abstractmethod
    async def replace_active_messages(
        self,
        messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        """Replace the active message list; preserve soft-deleted tombstones.

        All currently-active rows are removed and *messages* become the new
        active list.  Soft-deleted rows are NOT touched — they survive until
        :meth:`cleanup_expired` (TTL) or session deletion.  When
        *expected_revision* is provided and does not match, returns ``None``
        without modifying anything.
        """
        ...


class KVStore(ABC):
    """Scoped key/value record store.

    Owns arbitrary structured records keyed by string (scope metadata, archive
    state, plugin data).
    """

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        """Return the value for *key*, or ``None`` if absent."""
        ...

    @abstractmethod
    async def set(self, key: str, value: Any) -> None:
        """Set *key* to *value*, overwriting any prior value."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete *key*; return whether it existed."""
        ...

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        """Return keys whose name starts with *prefix* (empty prefix = all)."""
        ...


class CursorStore(ABC):
    """Monotonic processing cursors for a scoped memory layer.

    Named cursors track how far a consumer has read through an append-only
    stream (e.g. archive log replay, incremental consolidation).
    """

    @abstractmethod
    async def get_last_cursor(self, cursor_name: str = "default") -> int:
        """Return the last committed value for *cursor_name* (0 if unset)."""
        ...

    @abstractmethod
    async def set_last_cursor(self, cursor_name: str, cursor: int) -> None:
        """Advance *cursor_name* to *cursor* (monotonic; callers enforce ordering)."""
        ...


class ArchiveStore(ABC):
    """Append-only archive log store with per-channel partitioning.

    Owns the history-archive slice: the global log, the
    per-channel log, persisted archive state, and maintenance (prune + empty-dir
    cleanup). There is no separate ``LogStore`` ABC — all log methods live here.
    """

    @abstractmethod
    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Append *entry* to the archive log; return the stored entry (with cursor)."""
        ...

    @abstractmethod
    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        """Return log entries with cursor > *since_cursor*, capped at *limit*."""
        ...

    @abstractmethod
    async def save_logs(self, entries: list[dict[str, Any]]) -> None:
        """Atomically replace the entire archive log with *entries*."""
        ...

    @abstractmethod
    async def read_archive_state(self) -> dict[str, Any] | None:
        """Return persisted archive generation state, or ``None`` if never written."""
        ...

    @abstractmethod
    async def write_archive_state(self, state: dict[str, Any]) -> None:
        """Persist archive generation *state*."""
        ...

    @abstractmethod
    async def append_channel_log(self, channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Append *entry* to *channel*'s partitioned log; return the stored entry."""
        ...

    @abstractmethod
    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        """Return *channel*'s entries with archive_id > *since_archive_id*, capped at *limit*."""
        ...

    @abstractmethod
    async def save_channel_logs(self, channel: str, entries: list[dict[str, Any]]) -> None:
        """Atomically replace *channel*'s log with *entries*."""
        ...

    @abstractmethod
    async def prune_to_max(self, max_entries: int) -> int:
        """Delete oldest entries until total <= *max_entries*; return the count removed."""
        ...

    @abstractmethod
    async def cleanup_empty_dirs(self) -> int:
        """Remove empty archive directories left after pruning; return the count removed."""
        ...


class MemoryStoreBundle(BaseModel):
    """Composition of the four split stores for one scoped memory layer.

    A bundle wires the three required stores (``messages`` / ``kv`` /
    ``cursors``) and an optional ``archive``. Sessions without archival history
    (e.g. ephemeral subagent sessions) pass ``archive=None``.

    Frozen Pydantic model: the store wiring is fixed at construction; callers
    swap stores by building a new bundle, never by mutating fields.
    ``arbitrary_types_allowed`` is required because the store ABCs are plain
    ABCs, not Pydantic types — Pydantic validates them via ``isinstance``.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    messages: MessageStore
    kv: KVStore
    cursors: CursorStore
    archive: ArchiveStore | None = None


class PoolRoutingStore(ABC):
    """Persistence interface for session-prefix to pool routing."""

    @abstractmethod
    def get_pool(self, session_prefix: str) -> str | None:
        """Return the routed pool, or ``None`` when no route exists."""
        ...

    @abstractmethod
    def set_pool(self, session_prefix: str, pool_name: str) -> None:
        """Persist the pool route for a session prefix."""
        ...

    @abstractmethod
    def delete_pool(self, session_prefix: str) -> None:
        """Delete the route for a session prefix when present."""
        ...

    @abstractmethod
    def list_prefixes(self) -> list[str]:
        """Return all stored session prefixes in deterministic order."""
        ...

    @abstractmethod
    def delete_pool_routes(self, pool_name: str) -> int:
        """Delete all routes pointing to *pool_name*. Returns count deleted."""
        ...

    def get(self, session_prefix: str, default: str | None = None) -> str | None:
        """Convenience alias: ``get_pool`` with a default fallback."""
        return self.get_pool(session_prefix) or default

    def set(self, session_prefix: str, pool_name: str) -> None:
        """Convenience alias: delegate to ``set_pool``."""
        self.set_pool(session_prefix, pool_name)

    def close(self) -> None:  # noqa: B027 - no-op default; resource-owning stores override
        """Release resources owned by this store.

        The no-op default suits the file-backed routing stores, which own no
        dedicated resources. Stores that own real resources (a shared SQLite
        connection; an OTEL_HTTP trace store's sender thread + OTLP client)
        override this and must be closed at teardown.
        """
        return None


class ScopedBundleFactory(ABC):
    """Port: construct a structured :class:`MemoryStoreBundle` for a record scope.

    The seam the memory domain's hybrid registry uses to reach DB-backed
    bundles: the persistence manager implements it, the memory registry
    consumes it, and neither package imports the other (W3b inversion —
    both sides meet in core).
    """

    @abstractmethod
    def create_bundle(
        self,
        scope: RecordScope,
        *,
        with_archive: bool = True,
        ttl_seconds: float | None = None,
    ) -> MemoryStoreBundle:
        """Construct a structured store bundle for *scope*.

        ``with_archive=False`` omits the archive store (sessions without
        archival history).
        """
        ...
