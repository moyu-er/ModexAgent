from __future__ import annotations

import time


class MessageDeduplicator:
    """基于 message_id 的消息去重器，支持 TTL 过期清理。"""

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 300.0):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def is_duplicate(self, message_id: str) -> bool:
        """检查 message_id 是否已在窗口内被见过。"""
        now = time.monotonic()
        if message_id in self._seen:
            self._seen[message_id] = now
            return True
        self._seen[message_id] = now
        self._prune_if_needed()
        return False

    def _prune_if_needed(self) -> None:
        """超过容量或 TTL 时清理最旧条目。"""
        now = time.monotonic()
        expired = [mid for mid, ts in self._seen.items() if now - ts > self._ttl_seconds]
        for mid in expired:
            self._seen.pop(mid, None)
        if len(self._seen) > self._max_size:
            sorted_items = sorted(self._seen.items(), key=lambda x: x[1])
            to_remove = len(self._seen) - self._max_size
            for mid, _ in sorted_items[:to_remove]:
                self._seen.pop(mid, None)

    def mark_seen(self, message_id: str) -> None:
        """显式标记 message_id 为已见（不返回是否重复）。"""
        self._seen[message_id] = time.monotonic()
        self._prune_if_needed()
