"""Workspace- and pool-partitioned transcript store (ctxvar-routed writes).

This module lives in ``bot.service`` (the central wiring hub) because
workspace/pool partitioning is a cross-cutting **business** concern shared by
every channel (WebUI and IM) — it belongs to neither.

The default file adapter uses a self-documenting physical partition::

    <sessions_dir>/<pool>/{full_session_id}.jsonl

The store is keyed by the **full session id** (the receiver-owned identifier
``{conv}.{agent}[.{invocation_id}]`` shared with the memory system), so two
subagent invocations of the same agent persist to separate files.

Routing is driven by the per-turn ``bind_workspace_root`` ctxvar
(:mod:`modex_agent.workspace.runtime`):

- **Writes** (``append``) resolve the owning ``<root>/<data_dir_name>/sessions``
  from :func:`resolve_workspace_root`. The dispatcher already binds the turn's
  workspace root; the framework emitter therefore needs no workspace argument.
  The only writer outside a bound turn — the input pipeline's
  :class:`PersistUserMessageStage` — binds the envelope's workspace around its
  append.
- **Reads** accept an optional ``sessions_dir: Path | None`` override so HTTP
  request handlers (which run outside any turn) can pass the ``?ws=``-resolved
  directory explicitly; in-turn reads fall back to the ctxvar root.

The framework (agent emitter, IM FanIn) writes through this store
transparently.  It only ever calls ``append(session_id, event)``; the
dispatcher resolves the owning pool for that session — pool is derived from
the agent segment of the session id via the pool map — and routes the write
to the matching physical store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import pathvalidate

from bot.webui.events import ServerEvent
from bot.webui.transcript_store import (
    JSONLTranscriptStore,
    ResilientTranscriptStore,
    TranscriptStore,
)
from bot.webui.types import WorkspaceIndex
from modex_agent.core.session_id import agent_of, session_id_prefix_of
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import is_workspace_root_bound, resolve_workspace_root

WorkspaceTranscriptStoreResolver = Callable[[Path], Awaitable[TranscriptStore]]

logger = logging.getLogger(__name__)

_DEFAULT_POOL: str = "main"


def _pool_sanitized(pool: str) -> str:
    """Sanitize a pool name to a filesystem-safe directory name."""
    if not pool:
        return _DEFAULT_POOL
    sanitized = pathvalidate.sanitize_filename(pool, replacement_text="_")
    return sanitized.strip("_") or _DEFAULT_POOL


def _agent_of(session_id: str) -> str:
    """Return the agent segment (2nd) of a full session id.

    ``{conv}.{agent}[.{invocation_id}]`` → ``agent``.  Defaults to ``main``
    for malformed ids without an agent segment.
    """
    return agent_of(session_id, default=_DEFAULT_POOL)


def _conversation_prefix(session_id: str) -> str:
    """Return the conversation prefix (segment before the first ``.``)."""
    return session_id_prefix_of(session_id)


class _FileWorkspaceTranscriptStore(TranscriptStore):
    """One workspace's pool-partitioned JSONL transcript adapter."""

    def __init__(
        self,
        sessions_dir: Path,
        pool_for_agent: Callable[[str], str],
    ) -> None:
        self._sessions_dir = sessions_dir
        self._pool_for_agent = pool_for_agent

    def _store_for(self, pool: str) -> TranscriptStore:
        return ResilientTranscriptStore(
            JSONLTranscriptStore(self._sessions_dir / _pool_sanitized(pool))
        )

    def _pools(self) -> list[str]:
        if not self._sessions_dir.is_dir():
            return []
        return sorted(path.name for path in self._sessions_dir.iterdir() if path.is_dir())

    async def _owner(self, session_id: str) -> TranscriptStore:
        for pool in self._pools():
            store = self._store_for(pool)
            if session_id in await store.list_sessions():
                return store
        return self._store_for(self._pool_for_agent(_agent_of(session_id)))

    async def append(
        self,
        session_id: str,
        event: ServerEvent,
        *,
        pool: str = _DEFAULT_POOL,
    ) -> None:
        owner = pool if pool != _DEFAULT_POOL else self._pool_for_agent(_agent_of(session_id))
        await self._store_for(owner).append(session_id, event, pool=_pool_sanitized(owner))

    async def load(self, session_id: str) -> list[ServerEvent]:
        return await (await self._owner(session_id)).load(session_id)

    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
    ) -> list[ServerEvent]:
        stores = (
            [self._store_for(pool)]
            if pool is not None
            else [self._store_for(name) for name in self._pools()]
        )
        events = [
            event
            for store in stores
            for event in await store.load_sessions_by_prefix(session_prefix)
        ]
        events.sort(key=lambda event: event.timestamp)
        return events

    async def list_sessions(self) -> set[str]:
        sessions: set[str] = set()
        for pool in self._pools():
            sessions.update(await self._store_for(pool).list_sessions())
        return sessions

    async def list_sessions_by_prefix(self, session_prefix: str) -> set[str]:
        sessions: set[str] = set()
        for pool in self._pools():
            sessions.update(await self._store_for(pool).list_sessions_by_prefix(session_prefix))
        return sessions

    async def delete_session(self, session_id: str) -> None:
        await (await self._owner(session_id)).delete_session(session_id)

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        for pool in self._pools():
            await self._store_for(pool).delete_sessions_by_prefix(session_prefix)

    async def last_updated(self, session_id: str) -> int | None:
        return await (await self._owner(session_id)).last_updated(session_id)


class WorkspaceScopedTranscriptStore(TranscriptStore, WorkspaceIndex):
    """Route transcript operations to one backend adapter per workspace.

    The default resolver creates the file adapter. A persistence assembly may
    inject a resolver for SQLite, PostgreSQL, or another backend without this
    router learning about connections, migrations, or backend kinds.
    - ``append`` routes to the session's owning pool under the ctxvar-resolved
      workspace root.  Pool is derived from the agent segment of the session id
      via the pool map; subagents absent from the map inherit the main pool.
    - Ownership is read from the physical layout and cached in memory.  No
      ``sessions.json`` is written or read.

    The physical sessions directory is resolved from the bound workspace root
    (``<root>/<data_dir_name>/sessions``); reads can override it with an
    explicit ``sessions_dir`` so HTTP handlers do not depend on the ctxvar.
    """

    def __init__(
        self,
        data_dir_name: str,
        store_resolver: WorkspaceTranscriptStoreResolver | None = None,
    ) -> None:
        self._data_dir_name: str = data_dir_name
        # agent_name -> pool_name (main agents); set by the service after pools built.
        self._agent_pool_map: dict[str, str] = {}
        self._store_resolver = store_resolver
        self._workspace_stores: dict[Path, TranscriptStore] = {}
        self._inflight: dict[Path, asyncio.Task[TranscriptStore]] = {}
        self._generations: dict[Path, int] = {}
        # In-memory partial streaming buffer: key = "sessions_dir|session_id".
        # Single process-wide dict; cleared on turn_end, gone on crash.
        self._partial_buffer: dict[str, list[ServerEvent]] = {}

    def set_store_resolver(self, resolver: WorkspaceTranscriptStoreResolver) -> None:
        """Configure the persistence-owned workspace adapter resolver."""
        self._store_resolver = resolver

    def release_workspace(self, sessions_dir: Path) -> None:
        """Forget an evicted workspace before its adapter resources close."""
        key = sessions_dir.resolve()
        self._workspace_stores.pop(key, None)
        task = self._inflight.pop(key, None)
        if task is not None:
            task.cancel()
        self._generations[key] = self._generations.get(key, 0) + 1

    async def _workspace_store(self, sessions_dir: Path) -> TranscriptStore:
        key = sessions_dir.resolve()
        store = self._workspace_stores.get(key)
        if store is not None:
            return store
        task = self._inflight.get(key)
        if task is None:
            generation = self._generations.get(key, 0)
            task = asyncio.create_task(self._resolve_workspace_store(key, generation))
            self._inflight[key] = task
        return await asyncio.shield(task)

    async def _resolve_workspace_store(
        self, sessions_dir: Path, generation: int
    ) -> TranscriptStore:
        try:
            resolver = self._store_resolver
            store = (
                await resolver(sessions_dir)
                if resolver is not None
                else _FileWorkspaceTranscriptStore(sessions_dir, self._pool_for_agent)
            )
            if self._generations.get(sessions_dir, 0) == generation:
                self._workspace_stores[sessions_dir] = store
            return store
        finally:
            current = self._inflight.get(sessions_dir)
            if current is asyncio.current_task():
                self._inflight.pop(sessions_dir, None)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set agent_name -> pool_name mapping (main agents)."""
        self._agent_pool_map = dict(mapping)

    # ------------------------------------------------------------------
    # Directory resolution
    # ------------------------------------------------------------------

    def _ctxvar_sessions_dir(self) -> Path:
        """Resolve the sessions dir from the bound workspace root (ctxvar)."""
        root = resolve_workspace_root()
        return WorkspacePaths(root=root / self._data_dir_name).sessions_dir

    def _resolve_dir(self, sessions_dir: Path | None) -> Path:
        """Return the explicit dir when given, else the ctxvar-resolved dir."""
        if sessions_dir is not None:
            return sessions_dir
        return self._ctxvar_sessions_dir()

    def sessions_dir_for_session(self, session_id: str) -> Path:
        """Return the resolved sessions directory for *session_id*.

        Uses the ctxvar root — appropriate for in-turn callers.  HTTP handlers
        should pass an explicit ``sessions_dir`` to the read methods instead.
        """
        return self._ctxvar_sessions_dir()

    def _pool_for_agent(self, agent: str) -> str:
        """Resolve the pool for an agent from the configured map.

        The map (set by the service from pool configs + subagent templates)
        covers main agents, resident subagents, and dynamic-subagent template
        types.  Dynamic instances named ``{type}-{id}`` match by prefix.
        Unknown agents default to the main pool.
        """
        if agent in self._agent_pool_map:
            return self._agent_pool_map[agent]
        # Dynamic subagent: "{template_type}-{invocation_id}".
        for tmpl_type, pool in self._agent_pool_map.items():
            if "-" in tmpl_type:
                continue
            if agent.startswith(f"{tmpl_type}-"):
                return pool
        return _DEFAULT_POOL

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    async def append(
        self,
        session_id: str,
        event: ServerEvent,
        *,
        pool: str = _DEFAULT_POOL,
        sessions_dir: Path | None = None,
    ) -> None:
        # Workspace resolution: prefer an explicit ``sessions_dir`` (resolver-cell
        # driven, the same source memory uses — survives the broker-queue task
        # boundary). Only when none is given do we fall back to the bound ctxvar
        # root; in that fallback, an unbound root would silently land under
        # Path.cwd()/.modex (home/cwd), so surface it loudly.
        if sessions_dir is not None:
            resolved = sessions_dir
        else:
            if not is_workspace_root_bound():
                logger.warning(
                    "[ws-partition] transcript append for %s with NO bound workspace "
                    "root — writing under %s (cwd/home). This is expected only outside "
                    "a turn; a turn-time writer must run inside bind_workspace_root() "
                    "or pass an explicit sessions_dir.",
                    session_id,
                    resolve_workspace_root(),
                )
            resolved = self._ctxvar_sessions_dir()
        pool_key = pool if pool != _DEFAULT_POOL else self._pool_for_agent(_agent_of(session_id))
        await (await self._workspace_store(resolved)).append(
            session_id,
            event,
            pool=_pool_sanitized(pool_key),
        )

    async def load(self, session_id: str, sessions_dir: Path | None = None) -> list[ServerEvent]:
        resolved = self._resolve_dir(sessions_dir)
        return await (await self._workspace_store(resolved)).load(session_id)

    async def load_sessions_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
        sessions_dir: Path | None = None,
    ) -> list[ServerEvent]:
        resolved = self._resolve_dir(sessions_dir)
        return await (await self._workspace_store(resolved)).load_sessions_by_prefix(
            session_prefix,
            pool=_pool_sanitized(pool) if pool is not None else None,
        )

    async def list_sessions(self, sessions_dir: Path | None = None) -> set[str]:
        resolved = self._resolve_dir(sessions_dir)
        return await (await self._workspace_store(resolved)).list_sessions()

    async def list_sessions_by_prefix(
        self, session_prefix: str, sessions_dir: Path | None = None
    ) -> set[str]:
        resolved = self._resolve_dir(sessions_dir)
        return await (await self._workspace_store(resolved)).list_sessions_by_prefix(session_prefix)

    async def delete_session(self, session_id: str, sessions_dir: Path | None = None) -> None:
        resolved = self._resolve_dir(sessions_dir)
        await (await self._workspace_store(resolved)).delete_session(session_id)

    async def delete_sessions_by_prefix(
        self, session_prefix: str, sessions_dir: Path | None = None
    ) -> None:
        resolved = self._resolve_dir(sessions_dir)
        await (await self._workspace_store(resolved)).delete_sessions_by_prefix(session_prefix)

    async def last_updated(self, session_id: str, sessions_dir: Path | None = None) -> int | None:
        resolved = self._resolve_dir(sessions_dir)
        return await (await self._workspace_store(resolved)).last_updated(session_id)

    async def load_materialized_by_prefix(
        self,
        session_prefix: str,
        *,
        pool: str | None = None,
        sessions_dir: Path | None = None,
    ) -> list:
        """Materialize events for *session_prefix* into merged turn blocks."""
        from bot.webui.transcript_store import _materialize_events

        events = await self.load_sessions_by_prefix(
            session_prefix, sessions_dir=sessions_dir, pool=pool
        )
        return _materialize_events(events)

    # ------------------------------------------------------------------
    # Partial streaming events — in-memory, cleared on turn_end, no crash leftover.
    # ------------------------------------------------------------------

    async def append_partial(
        self,
        session_id: str,
        event: ServerEvent,
        *,
        sessions_dir: Path | None = None,
    ) -> None:
        resolved = self._resolve_dir(sessions_dir)
        key = self._partial_key(resolved, session_id)
        self._partial_buffer.setdefault(key, []).append(event)

    async def load_partial(
        self, session_id: str, sessions_dir: Path | None = None
    ) -> list[ServerEvent]:
        resolved = self._resolve_dir(sessions_dir)
        key = self._partial_key(resolved, session_id)
        return list(self._partial_buffer.get(key, ()))

    async def clear_partial(self, session_id: str, sessions_dir: Path | None = None) -> None:
        resolved = self._resolve_dir(sessions_dir)
        key = self._partial_key(resolved, session_id)
        self._partial_buffer.pop(key, None)

    @staticmethod
    def _partial_key(sessions_dir: Path, session_id: str) -> str:
        return f"{sessions_dir.resolve()}|{session_id}"
