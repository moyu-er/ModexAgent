"""SessionRegistry — runtime cache for SessionId resolution.

The store is authoritative; the registry is a performance cache that writes
through on register and loads from the store at startup. All operations are
async and guarded by an asyncio.Lock for concurrency safety.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from framework.core.session_id import SessionId
from framework.core.session_store import SessionStore


class SessionRegistry(ABC):
    """Runtime cache for SessionId lookups."""

    @abstractmethod
    async def register(self, session: SessionId) -> None:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None:
        ...

    @abstractmethod
    async def touch(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def load_all(self) -> None:
        ...


class InMemorySessionRegistry(SessionRegistry):
    """In-memory cache backed by an optional SessionStore."""

    def __init__(self, store: SessionStore | None = None) -> None:
        self._store = store
        self._cache: dict[str, SessionId] = {}
        self._lock = asyncio.Lock()

    async def load_all(self) -> None:
        if self._store is None:
            return
        async with self._lock:
            for session in await self._store.list_sessions():
                self._cache[str(session)] = session

    async def register(self, session: SessionId) -> None:
        async with self._lock:
            self._cache[str(session)] = session
        if self._store is not None:
            await self._store.save(session)

    async def get(self, session_id: str) -> SessionId | None:
        async with self._lock:
            return self._cache.get(session_id)

    async def touch(self, session_id: str) -> None:
        async with self._lock:
            session = self._cache.get(session_id)
            if session is not None:
                self._cache[session_id] = session.touch()
