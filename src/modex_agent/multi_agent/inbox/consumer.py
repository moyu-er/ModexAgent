"""Inbox 消息消费端。"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence.session_registry import InMemorySessionRegistry, SessionRegistry

from .server import InboxMQ
from .types import SESSION_WORK_METADATA_KEY, InboxMessage, SessionWork


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

    @abstractmethod
    async def acknowledge(self, session_id: str, message_id: str) -> None:
        """Retire a receipt only after receiver processing succeeds."""
        ...

    @abstractmethod
    def release(self, session_id: str, message_ids: list[str]) -> None:
        """Release live claims while retaining unacknowledged receipts."""
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
        self._registry: SessionRegistry = InMemorySessionRegistry()
        self._claimed: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    def set_session_registry(self, registry: SessionRegistry) -> None:
        """Use the pool's persisted session metadata for all receive paths."""
        self._registry = registry

    async def pending_work(self, session_id: str) -> SessionWork:
        info = await self._registry.get(session_id)
        data = info.metadata.get(SESSION_WORK_METADATA_KEY) if info is not None else None
        return SessionWork.model_validate(data) if data is not None else SessionWork()

    async def _save_work(self, session_id: str, work: SessionWork) -> None:
        await self._registry.register(SessionInfo.from_str(session_id).model_copy(update={
            "metadata": {SESSION_WORK_METADATA_KEY: work.model_dump(mode="json")},
        }))

    async def acknowledge(self, session_id: str, message_id: str) -> None:
        """Retire one receipt after the receiver's turn/history append succeeds."""
        async with self._lock:
            work = await self.pending_work(session_id)
            message = next((msg for msg in work.pending if msg.message_id == message_id), None)
            if message is None:
                return
            await self._save_work(session_id, SessionWork(pending=tuple(
                msg for msg in work.pending if msg.message_id != message_id
            )))
            self._touch_cache(self._cache_key(session_id, message_id))
            self.release(session_id, [message_id])
            if self._on_consumed is not None:
                await self._on_consumed(session_id, message)

    def release(self, session_id: str, message_ids: list[str]) -> None:
        """Release receipt claims in the receiver's finally; keep unacked work durable."""
        self._claimed.difference_update((session_id, message_id) for message_id in message_ids)

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
        """Reserve before destructive consume; caller acknowledges and finally releases.

        Claims separate a poller's held batch from nested fold-in receipts. The
        lock serializes receipt writes, never receiver execution or history I/O.
        """
        async with self._lock:
            work = await self.pending_work(session_id)
            queued = await self._server.peek(session_id)
            pending = {msg.message_id: msg for msg in (*work.pending, *queued)}
            result = [msg for msg in pending.values() if (
                (session_id, msg.message_id) not in self._claimed
                and self._cache_key(session_id, msg.message_id) not in self._cache
                and (not only_types or msg.message_type in only_types)
            )][:limit]
            ids = {msg.message_id for msg in result}
            self._claimed.update((session_id, message_id) for message_id in ids)
            try:
                saved = {msg.message_id: msg for msg in (*work.pending, *result)}
                if result:
                    await self._save_work(session_id, SessionWork(pending=tuple(saved.values())))
                queued_count = sum(msg.message_id in ids for msg in queued)
                if queued_count:
                    await self._server.consume(session_id, queued_count, only_types=only_types)
                return result
            except BaseException:
                self.release(session_id, list(ids))
                raise

    async def count(self, session_id: str) -> int:
        """返回待处理消息数量（非破坏性检查，不消费消息）。"""
        return await self._server.count(session_id)

    async def peek(self, session_id: str, limit: int = 1) -> list[InboxMessage]:
        """Read the authoritative pending view: saved receipts followed by queued intake."""
        work = await self.pending_work(session_id)
        queued = await self._server.peek(session_id)
        messages = {msg.message_id: msg for msg in (*work.pending, *queued)}
        return list(messages.values())[:limit]

    async def contains_pending(self, session_id: str, message_id: str) -> bool:
        return await self._server.contains_pending(session_id, message_id)

    async def sessions_with_pending(self) -> list[str]:
        """返回当前有 pending 消息（count > 0）的会话 ID 列表。"""
        return await self._server.sessions_with_pending()


# 显式别名保留多态替换能力
LocalCacheInboxConsumer = InboxConsumer
