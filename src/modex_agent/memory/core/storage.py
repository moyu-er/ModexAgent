"""Scoped memory storage abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from modex_agent.memory.core.lock import AioRWLock, StorageLock
from modex_agent.memory.core.models import StorageRevision


class MemoryStorage(ABC):
    """One concrete storage target for one memory layer and one scope."""

    def __init__(self, lock: StorageLock | None = None) -> None:
        self._lock = lock or AioRWLock()

    def get_lock(self) -> StorageLock:
        """Return this scoped storage instance's read/write lock."""
        return self._lock

    @property
    def base_path(self) -> Path | None:
        """Return the filesystem directory for this storage, or None if not file-backed."""
        return None

    @abstractmethod
    async def initialize(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    async def get(self, key: str) -> Any | None:
        pass

    @abstractmethod
    async def set(self, key: str, value: Any) -> None:
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        pass

    @abstractmethod
    async def list_keys(self, prefix: str = "") -> list[str]:
        pass

    @abstractmethod
    async def load_messages(self) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        pass

    async def append_message(self, message: dict[str, Any]) -> StorageRevision:
        messages = await self.load_messages()
        messages.append(message)
        return await self.save_messages(messages)

    @abstractmethod
    async def get_revision(self) -> StorageRevision:
        pass

    @abstractmethod
    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        pass

    @abstractmethod
    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        pass

    @abstractmethod
    async def save_logs(self, entries: list[dict[str, Any]]) -> None:
        pass

    @abstractmethod
    async def get_last_cursor(self, cursor_name: str = "default") -> int:
        pass

    @abstractmethod
    async def set_last_cursor(self, cursor_name: str, cursor: int) -> None:
        pass

    # -- archive maintenance extensions (default no-op implementations) ---------

    async def prune_to_max(self, max_total: int, min_safe_id: int = 0) -> int:
        """Delete oldest entries exceeding max_total. Default: no-op."""
        _ = max_total, min_safe_id
        return 0

    async def cleanup_empty_dirs(self) -> int:
        """Remove empty directories. Default: no-op."""
        return 0

    # -- archive channel extensions (default implementations) -------------------

    async def read_archive_state(self) -> dict[str, Any] | None:
        """Return persisted archive state. Default: read from KV store."""
        return await self.get(".archive_state")

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        """Persist archive state. Default: write to KV store."""
        await self.set(".archive_state", state)

    async def append_channel_log(self, channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        """Append entry to channel log. Default: delegate to append_log."""
        return await self.append_log({**entry, "channel": channel})

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        """Read channel logs with archive_id > since_archive_id.
        Default: filter read_logs by channel and archive_id.
        """
        entries = await self.read_logs(since_cursor=0, limit=limit)
        return [
            entry
            for entry in entries
            if entry.get("channel") == channel
            and int(entry.get("archive_id", 0) or 0) > since_archive_id
        ][:limit]

    async def save_channel_logs(self, channel: str, entries: list[dict[str, Any]]) -> None:
        """Atomically replace channel log. Default: merge via read_logs + save_logs."""
        all_entries = await self.read_logs(since_cursor=0, limit=1_000_000)
        other = [e for e in all_entries if e.get("channel") != channel]
        await self.save_logs(other + entries)
