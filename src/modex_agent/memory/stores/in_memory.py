"""In-memory storage backend for testing and ephemeral use."""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Any

from modex_agent.memory.core.lock import NoOpStorageLock, StorageLock
from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.scope import MemoryAgentRole, MemoryContext, ScopeRecord


class InMemoryStorage:
    """内存中的存储后端，使用嵌套字典实现。

    数据在进程重启后丢失，适合单元测试和开发调试。
    """

    def __init__(self, lock: StorageLock | None = None) -> None:
        self._lock = lock or NoOpStorageLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._changelogs: dict[str, list[dict[str, Any]]] = {}
        self._cursors: dict[str, int] = {}
        self._scope_records: dict[str, ScopeRecord] = {}

    def get_lock(self, lock_key: str | None = None) -> StorageLock:
        _ = lock_key
        return self._lock

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_revision(self, scope_key: str = "default") -> StorageRevision:
        from datetime import UTC, datetime

        messages = await self.load_messages(scope_key)
        return StorageRevision(
            message_count=len(messages),
            updated_at=datetime.now(UTC),
            version=await self.get_last_cursor(scope_key, "default"),
        )

    def list_scopes(self) -> list[str]:
        """返回所有已创建的 scope key 列表。"""
        return list(self._data.keys())

    def _ensure_scope(self, scope_key: str) -> None:
        if scope_key not in self._data:
            self._data[scope_key] = {}
            self._logs[scope_key] = []
            self._changelogs[scope_key] = []
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

    async def append_changelog(self, scope_key: str, entry: dict[str, Any]) -> int:
        """Append a changelog entry to a separate store from history archive."""
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            cursor_key = f"{scope_key}:changelog"
            cursor = self._cursors.get(cursor_key, 0) + 1
            self._cursors[cursor_key] = cursor
            entry = {**entry, "cursor": cursor}
            self._changelogs[scope_key].append(entry)
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

    async def ensure_scope_metadata(
        self,
        scope_key: str,
        *,
        layer: str,
        context: MemoryContext,
        agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN,
        agent_id: str | None = None,
    ) -> None:
        async with self.get_lock().write():
            self._ensure_scope(scope_key)
            existing = self._scope_records.get(scope_key)
            created_at = existing.created_at if existing else None
            self._scope_records[scope_key] = ScopeRecord(
                scope_key=scope_key,
                layer=layer,
                context=context,
                storage_path=f"memory://{scope_key}",
                agent_role=str(agent_role),
                agent_id=agent_id,
                created_at=created_at,
                updated_at=time.time(),
            )

    async def list_scope_records(
        self,
        *,
        layer: str | None = None,
        has_file: str | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset({MemoryAgentRole.MAIN}),
    ) -> list[ScopeRecord]:
        async with self.get_lock().read():
            records = list(self._scope_records.values())
            if layer is not None:
                records = [record for record in records if record.layer == layer]
            if agent_roles is not None:
                allowed_roles = {str(role) for role in agent_roles}
                records = [record for record in records if str(record.agent_role) in allowed_roles]
            if has_file == "messages":
                records = [
                    record
                    for record in records
                    if bool(self._data.get(record.scope_key, {}).get("__messages__"))
                ]
            elif has_file == "history":
                records = [record for record in records if bool(self._logs.get(record.scope_key))]
            return records
