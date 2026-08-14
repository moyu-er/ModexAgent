"""Inbox 消息消费端。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from .server import InboxMQ
from .types import InboxMessage


class BaseInboxConsumer(ABC):
    """Inbox 消息消费端抽象基类。"""

    @abstractmethod
    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        """从 InboxMQ 消费消息，返回经过去重过滤后的消息列表。"""
        ...

    async def count(self, session_id: str) -> int:
        """返回待处理消息数量（非破坏性检查）。"""
        return 0

    async def peek(self, session_id: str, limit: int = 1) -> list[InboxMessage]:
        """Non-destructive read of up to ``limit`` pending messages.

        Default returns empty; override for a real implementation. Used by the
        bus/poller to inspect pending envelopes (e.g. the parent link) without
        consuming them.
        """
        return []

    async def sessions_with_pending(self) -> list[str]:
        """返回当前有 pending 消息（count > 0）的会话 ID 列表。"""
        return []


class InboxConsumer(BaseInboxConsumer):
    """Inbox 消息消费端（本地缓存安全网实现）。"""

    def __init__(self, server: InboxMQ, cache_size: int = 1000) -> None:
        self._server = server
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._cache_size = cache_size
        self._on_consumed: Callable[[str, InboxMessage], Awaitable[None]] | None = None

    def set_on_consumed(
        self,
        callback: Callable[[str, InboxMessage], Awaitable[None]] | None,
    ) -> None:
        self._on_consumed = callback

    def _cache_key(self, session_id: str, message_id: str) -> str:
        return f"{session_id}:{message_id}"

    def _touch_cache(self, key: str) -> bool:
        if key in self._cache:
            self._cache.move_to_end(key)
            return True
        self._cache[key] = True
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return False

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        """从 InboxMQ 消费消息，返回经过本地去重过滤后的消息列表。"""
        messages = await self._server.consume(session_id, limit, only_types=only_types)
        result = []
        for msg in messages:
            cache_key = self._cache_key(session_id, msg.message_id)
            if not self._touch_cache(cache_key):
                result.append(msg)
        if self._on_consumed is not None:
            for msg in result:
                await self._on_consumed(session_id, msg)
        return result

    async def count(self, session_id: str) -> int:
        """返回待处理消息数量（非破坏性检查，不消费消息）。"""
        return await self._server.count(session_id)

    async def peek(self, session_id: str, limit: int = 1) -> list[InboxMessage]:
        """Non-destructive read of up to ``limit`` pending messages (no dedup)."""
        messages = await self._server.peek(session_id)
        return messages[:limit]

    async def contains_pending(self, session_id: str, message_id: str) -> bool:
        return await self._server.contains_pending(session_id, message_id)

    async def sessions_with_pending(self) -> list[str]:
        """返回当前有 pending 消息（count > 0）的会话 ID 列表。"""
        return await self._server.sessions_with_pending()


# 显式别名保留多态替换能力
LocalCacheInboxConsumer = InboxConsumer
