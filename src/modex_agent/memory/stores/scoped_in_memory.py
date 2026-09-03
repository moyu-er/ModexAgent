"""Scoped in-memory storage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modex_agent.core.message import MessageRole
from modex_agent.memory.core.lock import AioRWLock, StorageLock
from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.split_stores import (
    ArchiveStore,
    CursorStore,
    KVStore,
    MessageStore,
)
from modex_agent.memory.core.store_metadata import StoreMetadata
from modex_agent.utils.time import now_ms


class InMemoryScopedStorage(StoreMetadata, MessageStore, KVStore, CursorStore, ArchiveStore):
    """In-memory storage for one layer/scope target.

    Implements all four split store ABCs plus :class:`StoreMetadata`.
    Data is held in process memory and lost on restart — suitable for unit
    tests and ephemeral sessions.
    """

    def __init__(self, lock: StorageLock | None = None) -> None:
        self._lock = lock or AioRWLock()
        self._kv: dict[str, Any] = {}
        self._messages: list[dict[str, Any]] = []
        self._logs: list[dict[str, Any]] = []
        self._cursors: dict[str, int] = {"default": 0}
        self._version = 0
        self._updated_at = now_ms()
        self._archive_state: dict[str, Any] | None = None
        self._channel_logs: dict[str, list[dict[str, Any]]] = {}

    def get_lock(self) -> StorageLock:
        """Return the shared read/write lock for this store instance."""
        return self._lock

    @property
    def base_path(self) -> Path | None:
        return None

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def _touch(self) -> None:
        self._version += 1
        self._updated_at = now_ms()

    async def get(self, key: str) -> Any | None:
        async with self.get_lock().read():
            return self._kv.get(key)

    async def set(self, key: str, value: Any) -> None:
        async with self.get_lock().write():
            self._kv[key] = value
            self._touch()

    async def delete(self, key: str) -> bool:
        async with self.get_lock().write():
            if key not in self._kv:
                return False
            del self._kv[key]
            self._touch()
            return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        async with self.get_lock().read():
            return [key for key in self._kv if key.startswith(prefix)]

    async def load_messages(self) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            return list(self._messages)

    async def load_all_messages(
        self,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        async with self.get_lock().read():
            messages = [
                message
                for message in self._messages
                if message.get("role") != str(MessageRole.COMPACT)
            ]
            if limit is None:
                return messages
            return messages[-limit:]

    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        async with self.get_lock().write():
            self._messages = list(messages)
            self._touch()
            return self._get_revision_unsafe()

    async def append_message(self, message: dict[str, Any]) -> StorageRevision:
        async with self.get_lock().write():
            self._messages.append(dict(message))
            self._touch()
            return self._get_revision_unsafe()

    async def get_revision(self) -> StorageRevision:
        async with self.get_lock().read():
            return self._get_revision_unsafe()

    def _get_revision_unsafe(self) -> StorageRevision:
        return StorageRevision(
            message_count=len(self._messages),
            updated_at=self._updated_at,
            version=self._version,
        )

    async def prune_messages(self, max_messages: int) -> tuple[int, list[dict[str, Any]]]:
        async with self.get_lock().write():
            if len(self._messages) <= max_messages:
                return 0, []
            keep_idx: set[int] = (
                set(range(len(self._messages))[-max_messages:]) if max_messages > 0 else set()
            )
            for i, message in enumerate(self._messages):
                if message.get("_pinned"):
                    keep_idx.add(i)
            pruned = [self._messages[i] for i in range(len(self._messages)) if i not in keep_idx]
            if not pruned:
                return 0, []
            self._messages = [
                self._messages[i] for i in range(len(self._messages)) if i in keep_idx
            ]
            self._touch()
            return len(pruned), pruned

    async def pin_message(self, message_id: str) -> None:
        async with self.get_lock().write():
            changed = False
            for message in self._messages:
                if message.get("id") == message_id or message.get("message_id") == message_id:
                    message["_pinned"] = True
                    changed = True
            if changed:
                self._touch()

    async def unpin_message(self, message_id: str) -> None:
        async with self.get_lock().write():
            changed = False
            for message in self._messages:
                if (
                    message.get("id") == message_id or message.get("message_id") == message_id
                ) and "_pinned" in message:
                    message.pop("_pinned", None)
                    changed = True
            if changed:
                self._touch()

    async def delete_message(self, message_id: str) -> bool:
        async with self.get_lock().write():
            remaining = [
                m
                for m in self._messages
                if m.get("id") != message_id and m.get("message_id") != message_id
            ]
            if len(remaining) == len(self._messages):
                return False
            self._messages = remaining
            self._touch()
            return True

    async def cleanup_expired(self) -> int:
        return 0

    async def retain_messages(
        self,
        keep_messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        async with self.get_lock().write():
            if expected_revision is not None:
                current = self._get_revision_unsafe()
                if (
                    current.message_count != expected_revision.message_count
                    or current.version != expected_revision.version
                ):
                    return None
            self._messages = list(keep_messages)
            self._touch()
            return self._get_revision_unsafe()

    async def replace_active_messages(
        self,
        messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        async with self.get_lock().write():
            if expected_revision is not None:
                current = self._get_revision_unsafe()
                if (
                    current.message_count != expected_revision.message_count
                    or current.version != expected_revision.version
                ):
                    return None
            tombstones = [m for m in self._messages if m.get("_deleted")]
            self._messages = tombstones + list(messages)
            self._touch()
            return self._get_revision_unsafe()

    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        async with self.get_lock().write():
            cursor = self._cursors.get("default", 0) + 1
            self._cursors["default"] = cursor
            stored = {
                **entry,
                "cursor": cursor,
                "entry_id": entry.get("entry_id") or cursor,
                "created_at": entry.get("created_at") or now_ms(),
            }
            self._logs.append(stored)
            self._touch()
            return dict(stored)

    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            return [entry for entry in self._logs if entry.get("cursor", 0) > since_cursor][:limit]

    async def save_logs(self, entries: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            self._logs = list(entries)
            if entries:
                self._cursors["default"] = max(int(entry.get("cursor", 0)) for entry in entries)
            self._touch()

    async def get_last_cursor(self, cursor_name: str = "default") -> int:
        async with self.get_lock().read():
            return self._cursors.get(cursor_name, 0)

    async def set_last_cursor(self, cursor_name: str, cursor: int) -> None:
        async with self.get_lock().write():
            self._cursors[cursor_name] = cursor
            self._touch()

    async def read_archive_state(self) -> dict[str, Any] | None:
        async with self.get_lock().read():
            return self._archive_state

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        async with self.get_lock().write():
            self._archive_state = dict(state)
            self._touch()

    async def append_channel_log(self, channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        async with self.get_lock().write():
            archive_id = int(entry.get("archive_id", 0) or 0)
            stored = {
                **entry,
                "cursor": archive_id,
                "entry_id": entry.get("entry_id") or archive_id,
                "created_at": entry.get("created_at") or now_ms(),
            }
            self._channel_logs.setdefault(channel, []).append(stored)
            self._touch()
            return dict(stored)

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            entries = self._channel_logs.get(channel, [])
            filtered = [
                entry
                for entry in entries
                if int(entry.get("archive_id", 0) or 0) > since_archive_id
            ]
            return filtered[:limit] if limit else filtered

    async def save_channel_logs(self, channel: str, entries: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            self._channel_logs[channel] = list(entries)
            self._touch()

    async def prune_to_max(self, max_entries: int) -> int:
        async with self.get_lock().write():
            if len(self._logs) <= max_entries:
                return 0
            pruned_count = len(self._logs) - max_entries
            self._logs = self._logs[-max_entries:] if max_entries > 0 else []
            self._touch()
            return pruned_count

    async def cleanup_empty_dirs(self) -> int:
        return 0
