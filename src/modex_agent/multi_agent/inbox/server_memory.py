"""基于内存的 Inbox Server 实现，用于测试。"""

import asyncio

from .server import InboxServer
from .types import InboxMessage


class InMemoryInboxServer(InboxServer):
    """基于内存的 Inbox Server 实现，用于测试。"""

    def __init__(self) -> None:
        self._pending: dict[str, list[InboxMessage]] = {}
        self._delivered_ids: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def receive(self, session_id: str, message: InboxMessage) -> bool:
        async with self._lock:
            delivered = self._delivered_ids.setdefault(session_id, set())
            if message.message_id in delivered:
                return False
            pending = self._pending.setdefault(session_id, [])
            if any(m.message_id == message.message_id for m in pending):
                return False
            pending.append(message)
            return True

    async def consume(self, session_id: str, limit: int = 100) -> list[InboxMessage]:
        async with self._lock:
            pending = self._pending.pop(session_id, [])
            msgs = pending[:limit]
            # 未消费完的消息需要放回 pending（仅当 limit 切分时）
            if len(pending) > limit:
                self._pending[session_id] = pending[limit:]
            delivered = self._delivered_ids.setdefault(session_id, set())
            for m in msgs:
                delivered.add(m.message_id)
            return msgs

    async def peek(self, session_id: str) -> list[InboxMessage]:
        async with self._lock:
            return list(self._pending.get(session_id, []))

    async def count(self, session_id: str) -> int:
        async with self._lock:
            return len(self._pending.get(session_id, []))

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._pending.pop(session_id, None)
            self._delivered_ids.pop(session_id, None)

    async def list_sessions(self) -> list[str]:
        async with self._lock:
            sessions = set(self._pending.keys())
            sessions.update(self._delivered_ids.keys())
            return list(sessions)
