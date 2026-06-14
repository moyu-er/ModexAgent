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
            self._cache.clear()
            for session in await self._store.list_sessions():
                self._cache[str(session)] = session

    async def register(self, session: SessionId) -> None:
        async with self._lock:
            existing = self._cache.get(str(session))
            if existing is not None:
                # Merge: keep existing richer fields; only update fields
                # the incoming session explicitly provides (non-None / non-empty).
                update: dict[str, object] = {}
                if session.parent_session_id is not None:
                    update["parent_session_id"] = session.parent_session_id
                if session.created_at is not None and existing.created_at is None:
                    update["created_at"] = session.created_at
                if session.updated_at is not None:
                    update["updated_at"] = session.updated_at
                if session.metadata:
                    merged_meta = dict(existing.metadata)
                    merged_meta.update(session.metadata)
                    update["metadata"] = merged_meta
                if update:
                    merged = existing.model_copy(update=update)
                    self._cache[str(session)] = merged
                    if self._store is not None:
                        await self._store.save(merged)
            else:
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
                updated = session.touch()
                self._cache[session_id] = updated
                if self._store is not None:
                    await self._store.save(updated)
