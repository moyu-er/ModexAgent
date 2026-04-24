"""MemoryStorage abstract base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Collection
from typing import Any

from framework.memory.core.lock import AioRWLock, StorageLock
from framework.memory.core.scope import MemoryAgentRole, MemoryContext, ScopeRecord


class MemoryStorage(ABC):
    """底层存储抽象，所有记忆层共享同一接口。

    实现类需要支持：
    - 消息列表的读写（供工作/短期记忆使用）
    - 日志的追加和读取（供 History Archive 使用）
    - KV 操作（供长期记忆/元数据使用）
    - 游标追踪（供增量消费使用）
    """

    def __init__(self, lock: StorageLock | None = None) -> None:
        self._lock = lock or AioRWLock()

    def get_lock(self, lock_key: str | None = None) -> StorageLock:
        """Return the storage-level lock for this instance.

        All public read/write methods should wrap operations with this lock.
        The lock_key argument is reserved for potential sharded-lock use;
        implementations that do not shard may ignore it.
        """
        return self._lock

    # --- 生命周期 ---
    @abstractmethod
    async def initialize(self) -> None:
        """初始化存储资源，如创建目录、建表等。"""
        pass

    async def ensure_scope_metadata(
        self,
        scope_key: str,
        *,
        layer: str,
        context: MemoryContext,
        agent_role: str | MemoryAgentRole = MemoryAgentRole.MAIN,
        agent_id: str | None = None,
    ) -> None:
        """Persist recoverable metadata for a scope.

        This is deliberately part of the storage abstraction so memory layers
        can stay replaceable. Stores without scan support may keep the default
        no-op implementation.
        """
        _ = scope_key, layer, context, agent_role, agent_id

    async def list_scope_records(
        self,
        *,
        layer: str | None = None,
        has_file: str | None = None,
        agent_roles: Collection[str | MemoryAgentRole] | None = frozenset(
            {MemoryAgentRole.MAIN}
        ),
    ) -> list[ScopeRecord]:
        """List recoverable scope records for background memory jobs.

        agent_roles defaults to {"main"} because medium/long/provider jobs
        must not process peer/subagent scopes. Pass None to include all roles.
        """
        _ = layer, has_file, agent_roles
        return []

    @abstractmethod
    async def close(self) -> None:
        """关闭存储资源。"""
        pass

    # --- 通用 KV 操作 (供长记忆/元数据使用) ---
    @abstractmethod
    async def get(self, scope_key: str, key: str) -> Any | None:
        """读取指定 scope_key 下的 key 值。"""
        pass

    @abstractmethod
    async def set(self, scope_key: str, key: str, value: Any) -> None:
        """设置指定 scope_key 下的 key 值。"""
        pass

    @abstractmethod
    async def delete(self, scope_key: str, key: str) -> bool:
        """删除指定 key，返回是否成功。"""
        pass

    @abstractmethod
    async def list_keys(self, scope_key: str, prefix: str = "") -> list[str]:
        """列出指定 scope_key 下匹配 prefix 的所有 key。"""
        pass

    # --- 消息列表操作 (供工作/短期记忆使用) ---
    @abstractmethod
    async def load_messages(self, scope_key: str) -> list[dict[str, Any]]:
        """加载指定 scope_key 下的完整消息列表。"""
        pass

    @abstractmethod
    async def save_messages(self, scope_key: str, messages: list[dict[str, Any]]) -> None:
        """覆盖保存指定 scope_key 下的消息列表。"""
        pass

    async def append_message(self, scope_key: str, message: dict[str, Any]) -> None:
        """追加单条消息到列表（默认实现先 load 再 save，子类可优化为真追加）。"""
        messages = await self.load_messages(scope_key)
        messages.append(message)
        await self.save_messages(scope_key, messages)

    # --- 追加日志操作 (供 History Archive 使用) ---
    @abstractmethod
    async def append_log(self, scope_key: str, entry: dict[str, Any]) -> int:
        """追加日志条目，返回自增 cursor。"""
        pass

    @abstractmethod
    async def read_logs(
        self,
        scope_key: str,
        since_cursor: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """读取 since_cursor 之后的日志条目（不包含 since_cursor）。"""
        pass

    async def save_logs(self, scope_key: str, entries: list[dict[str, Any]]) -> None:
        """覆盖保存日志条目（用于淘汰旧记录）。默认实现抛出 NotImplementedError。"""
        raise NotImplementedError("save_logs is not implemented by this storage")

    @abstractmethod
    async def get_last_cursor(self, scope_key: str, cursor_name: str = "default") -> int:
        """获取指定游标的最后位置。"""
        pass

    @abstractmethod
    async def set_last_cursor(self, scope_key: str, cursor_name: str, cursor: int) -> None:
        """设置指定游标的最后位置。"""
        pass
