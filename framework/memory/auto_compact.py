"""Auto-compaction for idle sessions.

Periodically scans scopes and truncates old messages for sessions
that have been idle beyond a threshold. Retains the most recent
messages and stores a resumption summary in KV storage.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from framework.memory.core.storage import MemoryStorage

logger = logging.getLogger(__name__)


class AutoCompactService:
    """空闲会话自动压缩服务。

    定期扫描所有 scope，对空闲时间超过阈值的会话进行消息截断，
    保留最近 N 条消息。被截断的消息不保留原始内容，仅生成
    `[Resumed Session]` 摘要存入 KV，供上层在重新激活时注入。
    """

    def __init__(
        self,
        storage: MemoryStorage,
        idle_threshold_seconds: float = 1800.0,
        keep_recent_messages: int = 8,
    ) -> None:
        self._storage = storage
        self._idle_threshold = idle_threshold_seconds
        self._keep_recent = keep_recent_messages

    def _list_scopes(self) -> list[str]:
        """获取所有已知的 scope key 列表。"""
        list_fn = getattr(self._storage, "list_scopes", None)
        if list_fn is not None:
            return list_fn()
        return []

    async def scan_once(self) -> list[str]:
        """执行一次扫描，压缩所有空闲超阈值的 scope。

        Returns:
            被压缩的 scope_key 列表
        """
        compacted: list[str] = []
        for scope_key in self._list_scopes():
            try:
                if await self._is_idle(scope_key):
                    if await self._compact(scope_key):
                        compacted.append(scope_key)
            except Exception:
                logger.exception("Auto-compact failed for scope %s", scope_key)
        return compacted

    async def _is_idle(self, scope_key: str) -> bool:
        """判断指定 scope 是否已超过空闲阈值。"""
        last_activity = await self._storage.get(scope_key, ".last_activity")
        if last_activity is None:
            return False
        if not isinstance(last_activity, (int, float)):
            return False
        idle_time = time.time() - last_activity
        return idle_time > self._idle_threshold

    async def _compact(self, scope_key: str) -> bool:
        """压缩单个 scope，保留最近 N 条消息。

        Returns:
            True if compaction actually happened
        """
        messages = await self._storage.load_messages(scope_key)
        if len(messages) <= self._keep_recent:
            return False

        kept = messages[-self._keep_recent :]
        pruned_count = len(messages) - len(kept)

        await self._storage.save_messages(scope_key, kept)

        summary = (
            f"[Resumed Session] {pruned_count} older messages were auto-compacted. "
            f"Retained the most recent {len(kept)} messages."
        )
        await self._storage.set(scope_key, ".auto_compact_summary", summary)

        # 更新 last_activity 为压缩时间，防止立即再次触发
        await self._storage.set(scope_key, ".last_activity", time.time())

        logger.info(
            "Auto-compacted scope %s: kept %d, pruned %d",
            scope_key,
            len(kept),
            pruned_count,
        )
        return True
