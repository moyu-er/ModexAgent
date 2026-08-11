"""In-memory ``InboxMQ`` implementation — for tests (T11).

Implements the full :class:`InboxMQ` contract including the sync
:meth:`deliver` and :meth:`reap_expired` (no-op).

The class name :class:`InMemoryInboxServer` is retained for backwards
compatibility; it now subclasses :class:`InboxMQ` (which :class:`InboxServer`
aliases).
"""

from __future__ import annotations

import asyncio

from .server import InboxMQ
from .types import InboxMessage


class InMemoryInboxServer(InboxMQ):
    """In-memory ``InboxMQ`` implementation for tests."""

    def __init__(self) -> None:
        self._pending: dict[str, list[InboxMessage]] = {}
        self._delivered_ids: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Async MQ surface
    # ------------------------------------------------------------------ #

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

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        async with self._lock:
            pending = self._pending.get(session_id, [])
            taken: list[InboxMessage]
            if only_types is None:
                taken = pending[:limit]
                self._pending[session_id] = pending[limit:]
            else:
                taken = []
                kept: list[InboxMessage] = []
                for m in pending:
                    if len(taken) < limit and m.message_type in only_types:
                        taken.append(m)
                    else:
                        kept.append(m)
                self._pending[session_id] = kept
            if not self._pending.get(session_id):
                self._pending.pop(session_id, None)
            delivered = self._delivered_ids.setdefault(session_id, set())
            for m in taken:
                delivered.add(m.message_id)
            return taken

    async def peek(self, session_id: str) -> list[InboxMessage]:
        async with self._lock:
            return list(self._pending.get(session_id, []))

    async def contains_pending(self, session_id: str, message_id: str) -> bool:
        async with self._lock:
            return any(
                message.message_id == message_id
                for message in self._pending.get(session_id, [])
            )

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

    async def sessions_with_pending(self) -> list[str]:
        async with self._lock:
            return [sid for sid, msgs in self._pending.items() if msgs]

    # ------------------------------------------------------------------ #
    # Sync delivery surface (CLI cross-process)
    # ------------------------------------------------------------------ #

    def deliver(self, session_id: str, message: InboxMessage) -> bool:
        """Sync in-memory delivery — directly appends to the pending list.

        No locking: the in-memory server is single-process; the sync path is
        used only by tests that exercise the ``deliver`` contract without an
        event loop.
        """
        delivered = self._delivered_ids.setdefault(session_id, set())
        if message.message_id in delivered:
            return False
        pending = self._pending.setdefault(session_id, [])
        if any(m.message_id == message.message_id for m in pending):
            return False
        pending.append(message)
        return True

    # ------------------------------------------------------------------ #
    # Lifecycle maintenance
    # ------------------------------------------------------------------ #

    async def reap_expired(self) -> int:
        """No-op for the in-memory backend (no TTL). Returns ``0``."""
        return 0
