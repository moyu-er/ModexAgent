"""Workspace- and pool-partitioned transcript store (write-dispatching).

This module lives in ``bot.service`` (the central wiring hub) because
workspace/pool partitioning is a cross-cutting **business** concern shared by
every channel (WebUI and IM) — it belongs to neither.

Design — physical partition, self-documenting::

    <base>/<pool>/{full_session_id}.jsonl

The store is keyed by the **full session id** (the receiver-owned identifier
``{conv}.{agent}[.{invocation_id}]`` shared with the memory system), so two
subagent invocations of the same agent persist to separate files.

The framework (agent emitter, IM FanIn) writes through this store
transparently.  It only ever calls ``append(session_id, event)``; the
dispatcher resolves the owning pool for that session — pool is derived from
the agent segment of the session id via the pool map — and routes the write
to the matching physical store.

The ``base_dir`` points directly to ``{workspace}/.modex/sessions/``, so
workspace path encoding is unnecessary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pathvalidate

from framework.core.session_id import agent_of, session_id_prefix_of

from bot.webui.events import ServerEvent
from bot.webui.transcript_store import JSONLTranscriptStore, TranscriptStore

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
    """Transcript store physically partitioned by pool.

    - One :class:`JSONLTranscriptStore` per pool, lazily created under
      ``<base>/<pool>/``.
    - ``append`` routes to the session's owning pool.  Pool is derived from
      the agent segment of the session id via the pool map; subagents absent
      from the map inherit the main pool.
    - Ownership is read from the physical layout and cached in memory.  No
      ``sessions.json`` is written or read.

    The ``base_dir`` already encodes the workspace (typically
    ``{workspace}/.modex/sessions/``), so there is no separate workspace path
    layer in the directory layout.
    """

    def __init__(
        self,
        meta_base_dir: Path | None,
        workspace_resolver: Callable[[], str],
    ) -> None:
        self._base: Path | None = meta_base_dir
        self._resolver: Callable[[], str] = workspace_resolver
        # agent_name -> pool_name (main agents); set by the service after pools built.
        self._agent_pool_map: dict[str, str] = {}
        # pool_key -> store
        self._stores: dict[str, JSONLTranscriptStore] = {}
        # session_id -> pool_key ownership cache
        self._owners: dict[str, str] = {}
        self._scanned: bool = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set agent_name -> pool_name mapping (main agents)."""
        self._agent_pool_map = dict(mapping)

    def rebase(self, new_base_dir: Path) -> None:
        """Atomically switch backing directory and invalidate cached state.

        Called during workspace switches (``cd``) so the store writes to the
        new ``{workspace}/.modex/sessions/`` directory instead of the old one.
        """
        logger.info("WorkspaceStore rebase: %s -> %s", self._base, new_base_dir)
        self._base = new_base_dir
        self._stores.clear()
        self._owners.clear()
        self._scanned = False

    # ------------------------------------------------------------------
    # Physical stores
    # ------------------------------------------------------------------

    def _dir_for(self, pool_key: str) -> Path:
        return (self._base if self._base is not None else Path()) / pool_key

    def _store_for(self, pool_key: str) -> JSONLTranscriptStore:
        if pool_key not in self._stores:
            self._stores[pool_key] = JSONLTranscriptStore(self._dir_for(pool_key))
        return self._stores[pool_key]

    def store_for(self, workspace: str, pool: str) -> TranscriptStore:
        """Return the physical store for *pool*.

        *workspace* is accepted for backward compatibility but no longer used
        for path construction — the base directory already encodes the workspace.
        """
        del workspace  # Backward compatibility; base dir already encodes the workspace.
        return self._store_for(_pool_sanitized(pool))

    def pools_in(self, workspace: str) -> list[str]:
        """Return pool directory names that exist under the base directory.

        *workspace* is accepted for backward compatibility but no longer used.
        """
        del workspace  # Backward compatibility; base dir already encodes the workspace.
        self._scan()
        if self._base is None or not self._base.is_dir():
            return []
        return sorted(p.name for p in self._base.iterdir() if p.is_dir())

    # ------------------------------------------------------------------
    # Ownership (sourced from the physical layout, cached in memory)
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Populate the session→pool cache from the on-disk layout."""
        if self._scanned:
            return
        self._scanned = True
        if self._base is None or not self._base.is_dir():
            return
        for pool_dir in sorted(self._base.iterdir()):
            if not pool_dir.is_dir():
                continue
            pool_key = pool_dir.name
            self._store_for(pool_key)
            for f in pool_dir.glob("*.jsonl"):
                self._owners[f.stem] = pool_key

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

    def workspace_of(self, session_id: str) -> str | None:
        """Return the workspace owning *session_id*.

        Since ``base_dir`` already encodes the workspace, this always returns
        the resolver's current value (or None if empty).
        """
        del session_id
        ws = self._resolver()
        return ws or None

    def _owner(self, session_id: str) -> str:
        """Owning pool_key; defaults to the agent's pool."""
        self._scan()
        if session_id in self._owners:
            return self._owners[session_id]
        return _pool_sanitized(self._pool_for_agent(_agent_of(session_id)))

    def _pools_for_active_workspace(self) -> list[str]:
        return self.pools_in(self._resolver())

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    def append(self, session_id: str, event: ServerEvent) -> None:
        pool_key = _pool_sanitized(self._pool_for_agent(_agent_of(session_id)))
        self._store_for(pool_key).append(session_id, event)

    def load(self, session_id: str) -> Iterator[ServerEvent]:
        pool_key = self._owner(session_id)
        yield from self._store_for(pool_key).load(session_id)

    def load_conversation(self, conversation_id: str) -> Iterator[ServerEvent]:
        for pool_key in self.pools_in(self._resolver()):
            yield from self._store_for(pool_key).load_conversation(conversation_id)

    def list_sessions(self) -> set[str]:
        seen: set[str] = set()
        for pool_key in self._pools_for_active_workspace():
            seen |= self._store_for(pool_key).list_sessions()
        return seen

    def list_sessions_in_conversation(self, conversation_id: str) -> set[str]:
        seen: set[str] = set()
        for pool_key in self.pools_in(self._resolver()):
            seen |= self._store_for(pool_key).list_sessions_in_conversation(conversation_id)
        return seen

    def delete_session(self, session_id: str) -> None:
        pool_key = self._owner(session_id)
        self._store_for(pool_key).delete_session(session_id)
        self._owners.pop(session_id, None)

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation from every pool."""
        for pool_key in self.pools_in(self._resolver()):
            self._store_for(pool_key).delete_conversation(conversation_id)
        # Drop any cached owners belonging to this conversation.
        for sid in [s for s in self._owners if _conversation_prefix(s) == conversation_id]:
            self._owners.pop(sid, None)

    def last_updated(self, session_id: str) -> int | None:
        """Return the last update timestamp for *session_id* in milliseconds."""
        pool_key = self._owner(session_id)
        return self._store_for(pool_key).last_updated(session_id)
