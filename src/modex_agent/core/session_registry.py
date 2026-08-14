"""SessionRegistry — runtime cache for SessionInfo resolution.

The store is authoritative; the registry is a performance cache that writes
through on register and loads from the store at startup. All operations are
async and guarded by an asyncio.Lock for concurrency safety.

``on_register`` is an optional async callback invoked when a **new** session
is registered (not on merge/update). It is the single convergence point for
side effects that must accompany every session creation — e.g. writing
``pool_routing`` so the WebUI session list can resolve the pool for sessions
created via graph scheduling, subagent dispatch, or WS attach. The callback
receives the ``SessionInfo`` and must not raise (errors are logged, not
propagated, so a callback failure never blocks session registration).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from modex_agent.core.session_id import SessionInfo, now_ms
from modex_agent.core.session_store import SessionStore

logger = logging.getLogger(__name__)

OnSessionRegistered = Callable[[SessionInfo], Awaitable[None]]


class SessionRegistry(ABC):
    """Runtime cache for SessionInfo lookups."""

    @abstractmethod
    async def register(self, session: SessionInfo) -> None:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionInfo | None:
        ...

    @abstractmethod
    async def touch(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def load_all(self) -> None:
        ...

    @abstractmethod
    async def cleanup(self, session_id: str) -> None:
        """Remove a session from the cache and persistent store."""
        ...


class InMemorySessionRegistry(SessionRegistry):
    """In-memory cache backed by an optional SessionStore."""

    def __init__(
        self,
        store: SessionStore | None = None,
        *,
        on_register: OnSessionRegistered | None = None,
    ) -> None:
        self._store = store
        self._cache: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
        self._on_register = on_register

    async def load_all(self) -> None:
        if self._store is None:
            return
        async with self._lock:
            self._cache.clear()
            for session in await self._store.list_sessions():
                self._cache[session.session_id] = session

    async def register(self, session: SessionInfo) -> None:
        async with self._lock:
            existing = self._cache.get(session.session_id)
            if existing is not None:
                # Merge: keep existing richer fields; only update fields
                # the incoming session explicitly provides (non-None / non-empty).
                update: dict[str, object] = {}
                # The established parent is authoritative: only fill it in when
                # missing. Blindly overwriting would let a phantom session that
                # reuses an invocation_id reparent (orphan) an existing
                # subagent — see test_register_does_not_reparent_existing_session.
                if (
                    session.parent_session_id is not None
                    and existing.parent_session_id is None
                ):
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
                    self._cache[session.session_id] = merged
                    if self._store is not None:
                        await self._store.save(merged)
            else:
                # New record: ensure timestamps are initialized so callers that
                # reconstruct a SessionInfo from its id (e.g. AgentPool._track_session
                # using SessionInfo.from_str) do not persist a null updated_at.
                if session.created_at is None or session.updated_at is None:
                    now = now_ms()
                    session = session.model_copy(update={
                        "created_at": session.created_at or now,
                        "updated_at": session.updated_at or now,
                    })
                self._cache[session.session_id] = session
                if self._store is not None:
                    await self._store.save(session)
                if self._on_register is not None:
                    try:
                        await self._on_register(session)
                    except Exception:
                        logger.exception(
                            "on_register callback failed for session %s",
                            session.session_id,
                        )

    async def get(self, session_id: str) -> SessionInfo | None:
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

    async def cleanup(self, session_id: str) -> None:
        async with self._lock:
            self._cache.pop(session_id, None)
            if self._store is not None:
                await self._store.delete(session_id)
