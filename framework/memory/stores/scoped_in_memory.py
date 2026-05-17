"""Scoped in-memory storage."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from framework.memory.core.lock import AioRWLock, StorageLock
from framework.memory.core.models import StorageRevision
from framework.memory.core.storage import MemoryStorage


class InMemoryScopedStorage(MemoryStorage):
    """In-memory storage for one layer/scope target."""

    def __init__(self, lock: StorageLock | None = None) -> None:
        super().__init__(lock or AioRWLock())
        self._kv: dict[str, Any] = {}
        self._messages: list[dict[str, Any]] = []
        self._logs: list[dict[str, Any]] = []
        self._cursors: dict[str, int] = {"default": 0}
        self._version = 0
        self._updated_at = datetime.now(UTC)
        self._archive_state: dict[str, Any] | None = None
        self._channel_logs: dict[str, list[dict[str, Any]]] = {}
        self._archive_cursors: dict[str, int] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def _touch(self) -> None:
        self._version += 1
        self._updated_at = datetime.now(UTC)

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

    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        async with self.get_lock().write():
            cursor = self._cursors.get("default", 0) + 1
            self._cursors["default"] = cursor
            stored = {
                **entry,
                "cursor": cursor,
                "entry_id": entry.get("entry_id") or cursor,
                "created_at": entry.get("created_at") or datetime.now(UTC).isoformat(),
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

    # --- Archive channel storage ---

    async def read_archive_state(self) -> dict[str, Any] | None:
        async with self.get_lock().read():
            return self._archive_state

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        async with self.get_lock().write():
            self._archive_state = dict(state)
            self._touch()

    async def append_channel_log(
        self, channel: str, entry: dict[str, Any]
    ) -> dict[str, Any]:
        async with self.get_lock().write():
            cursor = self._archive_cursors.get(channel, 0) + 1
            self._archive_cursors[channel] = cursor
            stored = {
                **entry,
                "cursor": cursor,
                "entry_id": entry.get("entry_id") or entry.get("archive_id") or cursor,
                "created_at": entry.get("created_at") or datetime.now(UTC).isoformat(),
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

    async def save_channel_logs(
        self, channel: str, entries: list[dict[str, Any]]
    ) -> None:
        async with self.get_lock().write():
            self._channel_logs[channel] = list(entries)
            if entries:
                self._archive_cursors[channel] = max(
                    int(entry.get("cursor", 0)) for entry in entries
                )
            self._touch()
