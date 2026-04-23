"""History archive manager."""

from datetime import UTC, datetime
from typing import Any

from framework.memory.core.base_managers import BaseHistoryArchiveManager
from framework.memory.core.scope import MemoryContext, MemoryScope
from framework.memory.core.storage import MemoryStorage
from framework.memory.history_search import HistorySearchStrategy, KeywordHistorySearch


class HistoryArchiveManager(BaseHistoryArchiveManager):
    """管理结构化的历史摘要，支持 cursor 驱动的增量消费。

    当条目数超过 max_entries 时，会自动淘汰最旧的记录。
    支持通过 search_strategy 进行语义/关键词检索。
    """

    def __init__(
        self,
        storage: MemoryStorage,
        scope: MemoryScope,
        max_entries: int | None = None,
        search_strategy: HistorySearchStrategy | None = None,
    ):
        self._storage = storage
        self._scope = scope
        self._max_entries = max_entries
        self._search_strategy = search_strategy or KeywordHistorySearch()

    async def _maybe_prune(self, context: MemoryContext) -> None:
        """如果超出 max_entries，删除最旧的条目。"""
        if self._max_entries is None:
            return
        scope_key = self._scope.get_scope_key(context)
        entries = await self._storage.read_logs(scope_key, since_cursor=0)
        if len(entries) <= self._max_entries:
            return
        # 重新写入，仅保留最新的 max_entries 条
        keep = entries[-self._max_entries :]
        # 注意：这里直接覆盖 history.jsonl
        await self._storage.save_logs(scope_key, keep)

    async def append(
        self,
        context: MemoryContext,
        summary: str,
        metadata: dict[str, Any],
    ) -> int:
        """追加一条历史摘要，返回自增 cursor。"""
        scope_key = self._scope.get_scope_key(context)
        timestamp = metadata.get("timestamp") or datetime.now(UTC).isoformat()
        entry = {
            "timestamp": timestamp,
            "summary": summary,
            "metadata": {**metadata, "timestamp": timestamp},
            "session_id": context.session_id,
        }
        cursor = await self._storage.append_log(scope_key, entry)
        await self._maybe_prune(context)
        return cursor

    async def get_unprocessed(
        self,
        context: MemoryContext,
        cursor_name: str = "dream",
    ) -> tuple[int, list[dict[str, Any]]]:
        """读取自上次 cursor 以来未处理的历史条目。

        Returns:
            (new_cursor, entries) — 如果没有新条目，new_cursor 等于当前 cursor
        """
        scope_key = self._scope.get_scope_key(context)
        since = await self._storage.get_last_cursor(scope_key, cursor_name)
        entries = await self._storage.read_logs(scope_key, since_cursor=since)
        new_cursor = max(e.get("cursor", 0) for e in entries) if entries else since
        return new_cursor, entries

    async def commit_cursor(
        self,
        context: MemoryContext,
        cursor_name: str,
        cursor: int,
    ) -> None:
        """提交 cursor，标记条目已处理。"""
        scope_key = self._scope.get_scope_key(context)
        await self._storage.set_last_cursor(scope_key, cursor_name, cursor)

    async def get_recent(
        self,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """读取最近 limit 条历史摘要（不移动 cursor）。"""
        scope_key = self._scope.get_scope_key(context)
        entries = await self._storage.read_logs(scope_key, since_cursor=0)
        return entries[-limit:] if limit else entries

    async def search(
        self,
        context: MemoryContext,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """检索与 query 最相关的历史摘要条目。

        Args:
            context: 记忆上下文
            query: 查询字符串（用于关键词匹配）
            limit: 最多返回条目数

        Returns:
            按相关性排序的条目列表
        """
        scope_key = self._scope.get_scope_key(context)
        entries = await self._storage.read_logs(scope_key, since_cursor=0)
        return await self._search_strategy.search(entries, query, limit)
