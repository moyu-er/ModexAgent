"""Long-term memory manager."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from framework.memory.core.consolidation import MemoryUpdate
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.core.storage import MemoryStorage

DEFAULT_FILES = {
    "soul": "SOUL.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
}


@dataclass
class MemoryChangeLog:
    """Append-only audit log entry for long-term memory changes."""

    op: Literal["add", "update", "remove"]
    key: str
    old_hash: str | None
    new_hash: str | None
    reason: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "key": self.key,
            "file": self.key,
            "old_hash": self.old_hash,
            "new_hash": self.new_hash,
            "reason": self.reason,
            "timestamp": self.timestamp,
        }


@dataclass
class LongTermMemory:
    """长期记忆内容容器。

    对外保持字符串字段；内部 metadata 存储在 _metadata 中。
    """

    soul: str = ""
    user: str = ""
    memory: str = ""
    custom: dict[str, str] = field(default_factory=dict)
    _metadata: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def get_metadata(self, key: str) -> dict[str, Any]:
        """获取指定 key 的 metadata（不存在时返回空 dict）。"""
        return dict(self._metadata.get(key, {}))


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

    async def ensure_defaults(
        self,
        context: MemoryContext,
        defaults: dict[str, str] | None = None,
    ) -> None:
        """首次访问时初始化长期记忆默认模板。

        Args:
            context: 记忆上下文
            defaults: 键值对，如 {"soul": "default soul", "user": ""}。
                      未提供的 key 使用空字符串。
        """
        scope_key = self._scope.get_scope_key(context)
        defaults = defaults or {}
        for key, file_name in self._files.items():
            existing = await self._storage.get(scope_key, file_name)
            if existing is None:
                await self._storage.set(
                    scope_key, file_name, defaults.get(key, "")
                )

    async def _get_raw(
        self, scope_key: str, key: str
    ) -> tuple[str, dict[str, Any] | None]:
        """兼容读取存储值：支持旧 string 格式和新 dict {"value": "..."} 格式。"""
        raw = await self._storage.get(scope_key, key)
        if isinstance(raw, dict) and "value" in raw:
            return str(raw.get("value") or ""), raw
        if isinstance(raw, str):
            return raw, None
        return "", None

    async def get_all(self, context: MemoryContext) -> LongTermMemory:
        """读取所有长期记忆文件内容及 metadata。"""
        await self.ensure_defaults(context)
        scope_key = self._scope.get_scope_key(context)
        custom: dict[str, str] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for key in await self._storage.list_keys(scope_key):
            if key not in self._files.values():
                value = await self._storage.get(scope_key, key)
                if isinstance(value, str):
                    custom[key] = value
                elif isinstance(value, dict) and "value" in value:
                    custom[key] = str(value.get("value") or "")
                elif key.endswith("._meta") and isinstance(value, dict):
                    file_name = key[:-6]  # strip "._meta"
                    metadata[file_name] = value
        soul, _ = await self._get_raw(scope_key, self._files["soul"])
        user, _ = await self._get_raw(scope_key, self._files["user"])
        memory, _ = await self._get_raw(scope_key, self._files["memory"])
        return LongTermMemory(
            soul=soul,
            user=user,
            memory=memory,
            custom=custom,
            _metadata=metadata,
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
            existing, _ = await self._get_raw(scope_key, file_name)

        from framework.memory.core.consolidation import MemoryUpdateMode

        mode = update.mode.lower()
        if mode == str(MemoryUpdateMode.SECTION_REPLACE):
            result = update.content
        elif mode == str(MemoryUpdateMode.REPLACE_TEXT):
            if update.search_text and update.search_text in existing:
                result = existing.replace(update.search_text, update.content, 1)
            else:
                # Fallback: append if search_text not found
                result = (
                    existing
                    + ("\n" if existing and not existing.endswith("\n") else "")
                    + update.content
                )
        elif mode == str(MemoryUpdateMode.REMOVE):
            if update.search_text and update.search_text in existing:
                result = existing.replace(update.search_text, "", 1)
            elif update.content and update.content in existing:
                result = existing.replace(update.content, "", 1)
            else:
                # Nothing matched; leave existing unchanged
                result = existing
        else:  # append / incremental — both append new content
            result = (
                existing
                + ("\n" if existing and not existing.endswith("\n") else "")
                + update.content
            )

        result = self._trim_to_max_lines(result)
        # Migrate old string format to new dict format on first update
        await self._storage.set(scope_key, file_name, {"value": result})
        # Record metadata + changelog
        await self._record_change(scope_key, file_name, update, result)
        return result

    async def _record_change(
        self,
        scope_key: str,
        file_name: str,
        update: MemoryUpdate,
        result: str,
    ) -> None:
        """Append a changelog entry and update key metadata."""
        ts = time.time()
        new_hash = hashlib.sha256(result.encode("utf-8")).hexdigest()[:16]
        # Metadata: updated_at per key
        meta_key = f"{file_name}._meta"
        try:
            meta = await self._storage.get(scope_key, meta_key)
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        old_hash = meta.get("content_hash")
        meta["updated_at"] = ts
        meta["last_mode"] = update.mode
        meta["content_hash"] = new_hash
        await self._storage.set(scope_key, meta_key, meta)
        # Changelog: append-only audit trail
        entry = MemoryChangeLog(
            op="update",
            key=file_name,
            old_hash=old_hash,
            new_hash=new_hash,
            reason=update.reason or "",
            timestamp=ts,
        )
        try:
            # Use separate changelog storage if available, otherwise fall back to append_log
            append_changelog = getattr(self._storage, "append_changelog", None)
            if append_changelog is not None:
                await append_changelog(scope_key, entry.to_dict())
            else:
                await self._storage.append_log(scope_key, entry.to_dict())
        except Exception:
            # Changelog failure must not break the update
            pass

    async def get_file(self, context: MemoryContext, file_key: str) -> str | None:
        """读取指定长期记忆文件。"""
        scope_key = self._scope.get_scope_key(context)
        file_name = self._files.get(file_key, file_key)
        value, _ = await self._get_raw(scope_key, file_name)
        return value if value else None

    async def clear(self, context: MemoryContext) -> None:
        """清空指定 scope 的所有长期记忆文件。"""
        scope_key = self._scope.get_scope_key(context)
        for key in await self._storage.list_keys(scope_key):
            await self._storage.delete(scope_key, key)
