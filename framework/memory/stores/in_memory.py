"""In-memory storage backend for testing and ephemeral use."""

from typing import Any

from framework.memory.core.lock import NoOpStorageLock, StorageLock
from framework.memory.core.storage import MemoryStorage


class InMemoryStorage(MemoryStorage):
    """内存中的存储后端，使用嵌套字典实现。

    数据在进程重启后丢失，适合单元测试和开发调试。
    """

    def __init__(self, lock: StorageLock | None = None) -> None:
        super().__init__(lock or NoOpStorageLock())
        self._data: dict[str, dict[str, Any]] = {}
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._cursors: dict[str, int] = {}

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def _ensure_scope(self, scope_key: str) -> None:
        if scope_key not in self._data:
            self._data[scope_key] = {}
            self._logs[scope_key] = []
            self._cursors[f"{scope_key}:default"] = 0

    # --- KV ---
    async def get(self, scope_key: str, key: str) -> Any | None:
        async with self.get_lock().read():
            self._ensure_scope(scope_key)
            return self._data[scope_key].get(key)

    async def set(self, scope_key: str, key: str, value: Any) -> None:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            self._data[scope_key][key] = value

    async def delete(self, scope_key: str, key: str) -> bool:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            if key in self._data[scope_key]:
                del self._data[scope_key][key]
                return True
            return False

    async def list_keys(self, scope_key: str, prefix: str = "") -> list[str]:
        async with self.get_lock().read():
            self._ensure_scope(scope_key)
            return [k for k in self._data[scope_key] if k.startswith(prefix)]

    # --- Messages ---
    async def load_messages(self, scope_key: str) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            self._ensure_scope(scope_key)
            key = "__messages__"
            return list(self._data[scope_key].get(key, []))

    async def save_messages(self, scope_key: str, messages: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            self._data[scope_key]["__messages__"] = list(messages)

    async def append_message(self, scope_key: str, message: dict[str, Any]) -> None:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            key = "__messages__"
            if key not in self._data[scope_key]:
                self._data[scope_key][key] = []
            self._data[scope_key][key].append(message)

    # --- Logs ---
    async def append_log(self, scope_key: str, entry: dict[str, Any]) -> int:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            cursor_key = f"{scope_key}:default"
            cursor = self._cursors.get(cursor_key, 0) + 1
            self._cursors[cursor_key] = cursor
            entry = {**entry, "cursor": cursor}
            self._logs[scope_key].append(entry)
            return cursor

    async def read_logs(
        self,
        scope_key: str,
        since_cursor: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self.get_lock().read():
            self._ensure_scope(scope_key)
            logs = [e for e in self._logs[scope_key] if e.get("cursor", 0) > since_cursor]
            return logs[:limit]

    async def save_logs(self, scope_key: str, entries: list[dict[str, Any]]) -> None:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            self._logs[scope_key] = list(entries)

    async def get_last_cursor(self, scope_key: str, cursor_name: str = "default") -> int:
        async with self.get_lock().read():
            return self._cursors.get(f"{scope_key}:{cursor_name}", 0)

    async def set_last_cursor(self, scope_key: str, cursor_name: str, cursor: int) -> None:
        async with self.get_lock().write():
            self._cursors[f"{scope_key}:{cursor_name}"] = cursor
