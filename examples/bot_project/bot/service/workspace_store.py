"""Workspace- and pool-partitioned transcript store (ctxvar-routed writes).

This module lives in ``bot.service`` (the central wiring hub) because
workspace/pool partitioning is a cross-cutting **business** concern shared by
every channel (WebUI and IM) — it belongs to neither.

Design — physical partition, self-documenting::

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

import functools
import logging
from collections.abc import Iterator
from pathlib import Path

import pathvalidate

from bot.webui.events import ServerEvent
from bot.webui.transcript_store import (
    JSONLTranscriptStore,
    ResilientTranscriptStore,
    TranscriptStore,
)
from modex_agent.core.session_id import agent_of, session_id_prefix_of
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import is_workspace_root_bound, resolve_workspace_root

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


class WorkspaceScopedTranscriptStore(TranscriptStore):
    """Transcript store physically partitioned by pool, routed by ctxvar.

    - One :class:`JSONLTranscriptStore` per pool, lazily created under
      ``<sessions_dir>/<pool>/``.
    - ``append`` routes to the session's owning pool under the ctxvar-resolved
      workspace root.  Pool is derived from the agent segment of the session id
      via the pool map; subagents absent from the map inherit the main pool.
    - Ownership is read from the physical layout and cached in memory.  No
      ``sessions.json`` is written or read.

    The physical sessions directory is resolved from the bound workspace root
    (``<root>/<data_dir_name>/sessions``); reads can override it with an
    explicit ``sessions_dir`` so HTTP handlers do not depend on the ctxvar.
    """

    def __init__(self, data_dir_name: str) -> None:
        self._data_dir_name: str = data_dir_name
        # agent_name -> pool_name (main agents); set by the service after pools built.
        self._agent_pool_map: dict[str, str] = {}

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

    # ------------------------------------------------------------------
    # Physical stores
    # ------------------------------------------------------------------

    @staticmethod
    @functools.lru_cache(maxsize=64)
    def _store_for(sessions_dir: Path, pool_key: str) -> TranscriptStore:
        """Return a wrapped JSONL store for *sessions_dir* + *pool_key*.

        Stores are stateless wrappers, so the LRU cache is safe: evicted entries
        are recreated on next access with no loss of persisted data.
        """
        return ResilientTranscriptStore(
            JSONLTranscriptStore(sessions_dir / pool_key)
        )

    def store_for(self, sessions_dir: Path, pool: str) -> TranscriptStore:
        """Return the physical store for *sessions_dir* + *pool*."""
        return self._store_for(sessions_dir, _pool_sanitized(pool))

    def pools_in(self, sessions_dir: Path) -> list[str]:
        """Return pool directory names that exist under *sessions_dir*."""
        if not sessions_dir.is_dir():
            return []
        return sorted(p.name for p in sessions_dir.iterdir() if p.is_dir())

    # ------------------------------------------------------------------
    # Ownership (sourced from the physical layout, cached in memory)
    # ------------------------------------------------------------------

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

    def _owner_pool(self, sessions_dir: Path, session_id: str) -> str:
        """Owning pool_key; defaults to the agent's pool."""
        for pool_key in self.pools_in(sessions_dir):
            store = self._store_for(sessions_dir, pool_key)
            if session_id in store.list_sessions():
                return pool_key
        return _pool_sanitized(self._pool_for_agent(_agent_of(session_id)))

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    def append(
        self, session_id: str, event: ServerEvent, *, sessions_dir: Path | None = None
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
        pool_key = _pool_sanitized(self._pool_for_agent(_agent_of(session_id)))
        self._store_for(resolved, pool_key).append(session_id, event)

    def load(
        self, session_id: str, sessions_dir: Path | None = None
    ) -> Iterator[ServerEvent]:
        resolved = self._resolve_dir(sessions_dir)
        pool_key = self._owner_pool(resolved, session_id)
        yield from self._store_for(resolved, pool_key).load(session_id)

    def load_sessions_by_prefix(
        self,
        session_prefix: str,
        sessions_dir: Path | None = None,
        pool: str | None = None,
    ) -> Iterator[ServerEvent]:
        resolved = self._resolve_dir(sessions_dir)
        if pool is not None:
            yield from self.store_for(resolved, pool).load_sessions_by_prefix(session_prefix)
        else:
            for pool_key in self.pools_in(resolved):
                yield from self._store_for(resolved, pool_key).load_sessions_by_prefix(
                    session_prefix
                )

    def list_sessions(self, sessions_dir: Path) -> set[str]:
        seen: set[str] = set()
        for pool_key in self.pools_in(sessions_dir):
            seen |= self._store_for(sessions_dir, pool_key).list_sessions()
        return seen

    def list_sessions_by_prefix(
        self, session_prefix: str, sessions_dir: Path | None = None
    ) -> set[str]:
        resolved = self._resolve_dir(sessions_dir)
        seen: set[str] = set()
        for pool_key in self.pools_in(resolved):
            seen |= self._store_for(resolved, pool_key).list_sessions_by_prefix(
                session_prefix
            )
        return seen

    def delete_session(
        self, session_id: str, sessions_dir: Path | None = None
    ) -> None:
        resolved = self._resolve_dir(sessions_dir)
        pool_key = self._owner_pool(resolved, session_id)
        self._store_for(resolved, pool_key).delete_session(session_id)

    def delete_sessions_by_prefix(
        self, session_prefix: str, sessions_dir: Path | None = None
    ) -> None:
        resolved = self._resolve_dir(sessions_dir)
        for pool_key in self.pools_in(resolved):
            self._store_for(resolved, pool_key).delete_sessions_by_prefix(session_prefix)

    def last_updated(
        self, session_id: str, sessions_dir: Path | None = None
    ) -> int | None:
        resolved = self._resolve_dir(sessions_dir)
        pool_key = self._owner_pool(resolved, session_id)
        return self._store_for(resolved, pool_key).last_updated(session_id)

    def load_materialized_by_prefix(
        self,
        session_prefix: str,
        sessions_dir: Path | None = None,
        pool: str | None = None,
    ) -> list:
        """Materialize events for *session_prefix* into merged turn blocks."""
        from bot.webui.transcript_store import _materialize_events

        events: list[ServerEvent] = list(
            self.load_sessions_by_prefix(session_prefix, sessions_dir=sessions_dir, pool=pool)
        )
        return _materialize_events(events)
