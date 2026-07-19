"""Split memory store ABCs and ``MemoryStoreBundle``.

Four focused, deep ABCs, each owning one storage concern:

- :class:`MessageStore`  — conversation message history (9 methods)
- :class:`KVStore`       — scoped key/value records (4 methods)
- :class:`CursorStore`   — monotonic processing cursors (2 methods)
- :class:`ArchiveStore`  — append-only archive logs + channel logs (10 methods)

These four are composed by :class:`MemoryStoreBundle`, a frozen Pydantic model
holding the three required stores (``messages`` / ``kv`` / ``cursors``) and an
optional ``archive`` (sessions without archival history pass ``archive=None``).

All methods are async: the existing backends use async locks (see
``modex_agent.memory.core.lock``) and async file I/O.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from modex_agent.memory.core.models import StorageRevision


__all__ = [
    "ArchiveStore",
    "CursorStore",
    "KVStore",
    "MemoryStoreBundle",
    "MessageStore",
    "message_signature",
]

# Fields stripped before comparing two message dicts for identity.
# _pinned / _deleted are runtime markers added by load_messages/load_all_messages.
# reasoning_content / content_format are stripped by ChatMessage.to_dict().
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


class MessageStore(ABC):
    """Conversation message history for one scoped memory layer.

    Owns the short-term message list: load/save/append, revision tracking,
    pinning, pruning, deletion, and expired-message cleanup.

    **Soft-delete model.**  ``prune_messages`` and ``retain_messages``
    soft-delete (mark as deleted) rather than physically removing rows.
    ``load_messages`` returns only active messages; ``load_all_messages``
    returns including soft-deleted ones (used by context fork).  Physical
    removal happens via ``cleanup_expired`` (TTL) or ``delete_session_rows``
    (session deletion).
    """

    @abstractmethod
    async def load_messages(self) -> list[dict[str, Any]]:
        """Return active messages (excludes soft-deleted)."""
        ...

    @abstractmethod
    async def load_all_messages(self) -> list[dict[str, Any]]:
        """Return all messages including soft-deleted ones.

        Soft-deleted messages carry a ``_deleted: True`` marker.
        Used by context fork to access full parent history.
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
        """Soft-delete all active messages not in *keep_messages*.

        Messages whose content matches an entry in *keep_messages* stay
        active; every other active message is soft-deleted (preserved for
        :meth:`load_all_messages`).  When *expected_revision* is provided
        and does not match the current revision, returns ``None`` without
        modifying anything.
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
