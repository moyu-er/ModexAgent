"""Scoped memory storage abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from framework.memory.core.lock import AioRWLock, StorageLock
from framework.memory.core.models import StorageRevision


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
