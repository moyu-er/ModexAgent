"""Inbox 消息消费端。"""

from abc import ABC, abstractmethod
from collections import OrderedDict

from .server import InboxServer
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
        """从 InboxServer 消费消息，返回经过去重过滤后的消息列表。"""
        ...

    async def count(self, session_id: str) -> int:
        """返回待处理消息数量（非破坏性检查）。"""
        return 0

    async def sessions_with_pending(self) -> list[str]:
        """返回当前有 pending 消息（count > 0）的会话 ID 列表。"""
        return []


class InboxConsumer(BaseInboxConsumer):
    """Inbox 消息消费端（本地缓存安全网实现）。"""

    def __init__(self, server: InboxServer, cache_size: int = 1000) -> None:
        self._server = server
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._cache_size = cache_size

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
        """从 InboxServer 消费消息，返回经过本地去重过滤后的消息列表。"""
        messages = await self._server.consume(session_id, limit, only_types=only_types)
        result = []
        for msg in messages:
            cache_key = self._cache_key(session_id, msg.message_id)
            if not self._touch_cache(cache_key):
                result.append(msg)
        return result

    async def count(self, session_id: str) -> int:
        """返回待处理消息数量（非破坏性检查，不消费消息）。"""
        return await self._server.count(session_id)

    async def sessions_with_pending(self) -> list[str]:
        """返回当前有 pending 消息（count > 0）的会话 ID 列表。"""
        return await self._server.sessions_with_pending()


# 显式别名保留多态替换能力
LocalCacheInboxConsumer = InboxConsumer
