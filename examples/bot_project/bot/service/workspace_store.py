"""Workspace- and pool-partitioned transcript store (write-dispatching).

This module lives in ``bot.service`` (the central wiring hub) because
workspace/pool partitioning is a cross-cutting **business** concern shared by
every channel (WebUI and IM) — it belongs to neither.

Design — physical partition, self-documenting::

    <base>/<sanitized_ws>/<pool>/{full_session_id}.jsonl

The store is keyed by the **full session id** (the receiver-owned identifier
``{conv}.{agent}[.{invocation_id}]`` shared with the memory system), so two
subagent invocations of the same agent persist to separate files.

The framework (agent emitter, IM FanIn) writes through this store
transparently.  It only ever calls ``append(session_id, event)``; the
dispatcher resolves the owning (workspace, pool) for that session — workspace
is sticky (fixed at the first write), pool is derived from the agent segment
of the session id via the pool map — and routes the write to the matching
physical store.  Workspace is read lazily from ``workspace_resolver`` (the
shared ``WorkspaceContext``), so the framework never knows about workspaces
or pools.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pathvalidate

from bot.webui.events import ServerEvent
from bot.webui.transcript_store import JSONLTranscriptStore, TranscriptStore

_DEFAULT_POOL: str = "main"


def workspace_sanitized(workspace: str) -> str:
    """Sanitize a workspace path to a filesystem-safe directory name."""
    if not workspace:
        return "default"
    sanitized = pathvalidate.sanitize_filename(workspace, replacement_text="_")
    return sanitized.strip("_") or "default"


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
    parts = session_id.split(".", 2)
    return parts[1] if len(parts) >= 2 else _DEFAULT_POOL


def _conversation_prefix(session_id: str) -> str:
    """Return the conversation prefix (segment before the first ``.``)."""
    return session_id.split(".", 1)[0] if "." in session_id else session_id


class WorkspaceScopedTranscriptStore(TranscriptStore):
    """Transcript store physically partitioned by workspace and pool.

    - One :class:`JSONLTranscriptStore` per (workspace, pool), lazily created
      under ``<base>/<ws>/<pool>/``.
    - ``append`` routes to the session's owning (workspace, pool).  Workspace
      is sticky (fixed at first write; new sessions adopt the active
      workspace).  Pool is derived from the agent segment of the session id
      via the pool map; subagents absent from the map inherit the main pool.
    - Ownership is read from the physical layout and cached in memory.  No
      ``sessions.json`` is written or read.
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
        # (ws_key, pool_key) -> store
        self._stores: dict[tuple[str, str], JSONLTranscriptStore] = {}
        # session_id -> (ws_key, pool_key) ownership cache
        self._owners: dict[str, tuple[str, str]] = {}
        self._scanned: bool = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set agent_name -> pool_name mapping (main agents)."""
        self._agent_pool_map = dict(mapping)

    # ------------------------------------------------------------------
    # Physical stores
    # ------------------------------------------------------------------

    def _dir_for(self, ws_key: str, pool_key: str) -> Path:
        return (self._base if self._base is not None else Path()) / ws_key / pool_key

    def _store_for(self, ws_key: str, pool_key: str) -> JSONLTranscriptStore:
        key = (ws_key, pool_key)
        if key not in self._stores:
            self._stores[key] = JSONLTranscriptStore(self._dir_for(ws_key, pool_key))
        return self._stores[key]

    def store_for(self, workspace: str, pool: str) -> TranscriptStore:
        """Return the physical store for *workspace* + *pool* (plaintext names)."""
        return self._store_for(workspace_sanitized(workspace), _pool_sanitized(pool))

    def pools_in(self, workspace: str) -> list[str]:
        """Return pool directory names that exist under *workspace*."""
        self._scan()
        ws_key = workspace_sanitized(workspace)
        ws_dir = self._base / ws_key if self._base is not None else Path(ws_key)
        if not ws_dir.is_dir():
            return []
        return sorted(p.name for p in ws_dir.iterdir() if p.is_dir())

    # ------------------------------------------------------------------
    # Ownership (sourced from the physical layout, cached in memory)
    # ------------------------------------------------------------------

    def _scan(self) -> None:
        """Populate the session→(workspace,pool) cache from the on-disk layout."""
        if self._scanned:
            return
        self._scanned = True
        if self._base is None or not self._base.is_dir():
            return
        for ws_dir in sorted(self._base.iterdir()):
            if not ws_dir.is_dir():
                continue
            ws_key = ws_dir.name
            for pool_dir in sorted(ws_dir.iterdir()):
                if not pool_dir.is_dir():
                    continue
                pool_key = pool_dir.name
                self._store_for(ws_key, pool_key)
                for f in pool_dir.glob("*.jsonl"):
                    self._owners[f.stem] = (ws_key, pool_key)

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
        """Return the sanitized workspace key owning *session_id* (None if unknown)."""
        self._scan()
        owner = self._owners.get(session_id)
        return owner[0] if owner is not None else None

    def _owner(self, session_id: str) -> tuple[str, str]:
        """Owning (ws_key, pool_key); defaults to active workspace + agent's pool."""
        self._scan()
        if session_id in self._owners:
            return self._owners[session_id]
        ws_key = workspace_sanitized(self._resolver())
        pool_key = _pool_sanitized(self._pool_for_agent(_agent_of(session_id)))
        return (ws_key, pool_key)

    def _pools_for_active_workspace(self) -> list[str]:
        ws_key = workspace_sanitized(self._resolver())
        return self.pools_in(ws_key)

    # ------------------------------------------------------------------
    # TranscriptStore interface
    # ------------------------------------------------------------------

    def append(self, session_id: str, event: ServerEvent) -> None:
        # Always resolve the ACTIVE workspace — never cache/sticky.
        # IM messages (QQ, etc.) must follow cd workspace switches.
        ws_key = workspace_sanitized(self._resolver())
        pool_key = _pool_sanitized(self._pool_for_agent(_agent_of(session_id)))
        self._store_for(ws_key, pool_key).append(session_id, event)

    def load(self, session_id: str) -> Iterator[ServerEvent]:
        ws_key, pool_key = self._owner(session_id)
        yield from self._store_for(ws_key, pool_key).load(session_id)

    def load_conversation(self, conversation_id: str) -> Iterator[ServerEvent]:
        ws_key = workspace_sanitized(self._resolver())
        for pool_key in self.pools_in(ws_key):
            yield from self._store_for(ws_key, pool_key).load_conversation(conversation_id)

    def list_sessions(self) -> set[str]:
        seen: set[str] = set()
        for pool_key in self._pools_for_active_workspace():
            ws_key = workspace_sanitized(self._resolver())
            seen |= self._store_for(ws_key, pool_key).list_sessions()
        return seen

    def list_sessions_in_conversation(self, conversation_id: str) -> set[str]:
        seen: set[str] = set()
        ws_key = workspace_sanitized(self._resolver())
        for pool_key in self.pools_in(ws_key):
            seen |= self._store_for(ws_key, pool_key).list_sessions_in_conversation(conversation_id)
        return seen

    def delete_session(self, session_id: str) -> None:
        ws_key, pool_key = self._owner(session_id)
        self._store_for(ws_key, pool_key).delete_session(session_id)
        self._owners.pop(session_id, None)

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation from every pool of the active workspace."""
        ws_key = workspace_sanitized(self._resolver())
        for pool_key in self.pools_in(ws_key):
            self._store_for(ws_key, pool_key).delete_conversation(conversation_id)
        # Drop any cached owners belonging to this conversation.
        for sid in [s for s in self._owners if _conversation_prefix(s) == conversation_id]:
            self._owners.pop(sid, None)
