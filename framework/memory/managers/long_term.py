"""Long-term memory manager."""

from dataclasses import dataclass, field

from framework.memory.core.consolidation import MemoryUpdate
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.core.storage import MemoryStorage

DEFAULT_FILES = {
    "soul": "SOUL.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
}


@dataclass
class LongTermMemory:
    """长期记忆内容容器。"""

    soul: str = ""
    user: str = ""
    memory: str = ""
    custom: dict[str, str] = field(default_factory=dict)


from framework.memory.core.base_managers import BaseLongTermMemoryManager


class LongTermMemoryManager(BaseLongTermMemoryManager):
    """管理持久化的知识文件（SOUL.md / USER.md / MEMORY.md）。"""

    def __init__(
        self,
        storage: MemoryStorage,
        scope: MemoryScope,
        max_lines: int | None = None,
    ):
        self._storage = storage
        self._scope = scope
        self._files = dict(DEFAULT_FILES)
        self._max_lines = max_lines

    def _trim_to_max_lines(self, content: str) -> str:
        if self._max_lines is None:
            return content
        lines = content.split("\n")
        if len(lines) <= self._max_lines:
            return content

        kept = lines[-self._max_lines :]
        # 如果第一行不是 Markdown 标题，向前查找最近的标题行
        if kept and not kept[0].strip().startswith("#"):
            for i in range(len(lines) - self._max_lines - 1, -1, -1):
                if lines[i].strip().startswith("#"):
                    kept = lines[i:]
                    break
        return "\n".join(kept)

    async def get_all(self, context: MemoryContext) -> LongTermMemory:
        """读取所有长期记忆文件内容。"""
        scope_key = self._scope.get_scope_key(context)
        custom: dict[str, str] = {}
        for key in await self._storage.list_keys(scope_key):
            if key not in self._files.values():
                value = await self._storage.get(scope_key, key)
                if isinstance(value, str):
                    custom[key] = value
        return LongTermMemory(
            soul=(await self._storage.get(scope_key, self._files["soul"])) or "",
            user=(await self._storage.get(scope_key, self._files["user"])) or "",
            memory=(await self._storage.get(scope_key, self._files["memory"])) or "",
            custom=custom,
        )

    async def update(self, context: MemoryContext, updates: dict[str, str]) -> None:
        """批量更新长期记忆文件（全量覆盖模式）。

        Args:
            updates: 键值对，如 {"soul": "new content", "custom_key": "value"}
        """
        scope_key = self._scope.get_scope_key(context)
        for key, content in updates.items():
            file_name = self._files.get(key, key)
            await self._storage.set(scope_key, file_name, content)

    async def apply_update(
        self, context: MemoryContext, update: MemoryUpdate, existing: str | None = None
    ) -> str:
        """根据 MemoryUpdate 的模式合并内容并写回存储。

        Args:
            context: 记忆上下文
            update: 包含 mode/content 的更新指令
            existing: 可选的现有内容；未提供时从存储读取

        Returns:
            合并后的最终内容
        """
        scope_key = self._scope.get_scope_key(context)
        file_name = self._files.get(update.file_name, update.file_name)

        if existing is None:
            raw = await self._storage.get(scope_key, file_name)
            existing = raw if isinstance(raw, str) else ""

        mode = update.mode.lower()
        if mode == "section_replace":
            result = update.content
        else:  # append / incremental — both append new content
            result = (
                existing
                + ("\n" if existing and not existing.endswith("\n") else "")
                + update.content
            )

        result = self._trim_to_max_lines(result)
        await self._storage.set(scope_key, file_name, result)
        return result

    async def get_file(self, context: MemoryContext, file_key: str) -> str | None:
        """读取指定长期记忆文件。"""
        scope_key = self._scope.get_scope_key(context)
        file_name = self._files.get(file_key, file_key)
        value = await self._storage.get(scope_key, file_name)
        return value if isinstance(value, str) else None

    async def clear(self, context: MemoryContext) -> None:
        """清空指定 scope 的所有长期记忆文件。"""
        scope_key = self._scope.get_scope_key(context)
        for key in await self._storage.list_keys(scope_key):
            await self._storage.delete(scope_key, key)
