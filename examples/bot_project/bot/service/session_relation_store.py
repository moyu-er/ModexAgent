"""Persistent parent-child session relationship store.

Relations are stored per-pool in ``_relations.json``, co-located with
transcript JSONL files::

    .modex/sessions/<pool>/_relations.json

The ``base_dir`` already encodes the workspace (typically
``{workspace}/.modex/sessions/``), so there is no separate workspace path
layer in the directory layout.

The store is created by WebUIService and used by:
1. ``AgentCommunicationService._create_dynamic_subagent`` — writes parent relation at dispatch time
2. ``WebUIServer._handle_sessions`` — reads parent for API response
3. ``web_ui_service._resolve_session_meta`` — reads parent for DeltaEnvelope enrichment
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_POOL: str = "main"
_RELATIONS_FILENAME: str = "_relations.json"


def _conv_of(session_id: str) -> str:
    """Return the conversation prefix (segment before the first dot)."""
    return session_id.split(".", 1)[0] if "." in session_id else session_id


def _agent_of(session_id: str) -> str:
    """Return the agent segment (second) of a full session id.

    ``{conv}.{agent}[.{invocation_id}]`` → ``agent``.  Defaults to ``main``.
    """
    parts = session_id.split(".", 2)
    return parts[1] if len(parts) >= 2 else _DEFAULT_POOL


def _write_atomic(path: Path, data: dict[str, dict[str, object]]) -> None:
    """Atomically write *data* to *path* via temp-file + rename."""
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, dict[str, object]]:
    """Read and return parsed JSON from *path*, or empty dict if missing/corrupt."""
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return raw  # type: ignore[return-value]
    except (json.JSONDecodeError, OSError):
        pass
    return {}


class SessionRelationStore:
    """Persistent parent-child session relationship store.

    Relations are stored per-pool in ``<base_dir>/<pool>/_relations.json``.
    The ``base_dir`` encodes the workspace so no additional path layer is needed.

    The store holds an in-memory cache for fast reads; writes go through
    atomic file replacement.
    """

    def __init__(
        self,
        base_dir: Path,
        workspace_resolver: Callable[[], str] | None = None,
    ) -> None:
        self._base: Path = base_dir
        self._resolver: Callable[[], str] = workspace_resolver or (lambda: "")
        self._agent_pool_map: dict[str, str] = {}
        # child_session_id → parent_session_id (in-memory cache)
        self._parents: dict[str, str] = {}
        # parent_session_id → list[child_session_id] (sorted by created_at)
        self._children: dict[str, list[str]] = {}
        # child_session_id → created_at (millisecond int)
        self._created_at: dict[str, int] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set agent_name → pool_name mapping (all agents: main + subagents)."""
        self._agent_pool_map = dict(mapping)

    def rebase(self, new_base_dir: Path) -> None:
        """Atomically switch backing directory and invalidate cached state.

        Called during workspace switches (``cd``) so relations are read from
        and written to the new ``{workspace}/.modex/sessions/`` directory.
        """
        logger.info("SessionRelationStore rebase: %s -> %s", self._base, new_base_dir)
        self._base = new_base_dir
        self._loaded = False
        self._parents.clear()
        self._children.clear()
        self._created_at.clear()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_parent(self, child_session_id: str, parent_session_id: str) -> None:
        """Record *parent_session_id* as the parent of *child_session_id*."""
        self._ensure_loaded()
        now_ms = int(time.time() * 1000)
        self._parents[child_session_id] = parent_session_id
        self._created_at[child_session_id] = now_ms
        # Update children index
        if parent_session_id not in self._children:
            self._children[parent_session_id] = []
        children = self._children[parent_session_id]
        if child_session_id not in children:
            children.append(child_session_id)
        # Sort by created_at
        children.sort(key=lambda sid: self._created_at.get(sid, 0))
        self._persist_relations_for(child_session_id)

    def get_parent(self, session_id: str) -> str | None:
        """Return the parent session_id for *session_id*, or None.

        Checks: in-memory cache → on-disk persistence → derivation fallback.
        """
        self._ensure_loaded()
        # 1. In-memory cache
        if session_id in self._parents:
            return self._parents[session_id]
        # 2. On-disk (already loaded via _ensure_loaded)
        # 3. Derivation fallback
        return self._derive_parent(session_id)

    def get_children(self, parent_session_id: str) -> list[str]:
        """Return child session_ids sorted by created_at (oldest first).

        Returns an empty list when *parent_session_id* has no children.
        """
        self._ensure_loaded()
        return list(self._children.get(parent_session_id, []))

    def remove_session(self, session_id: str) -> None:
        """Remove a single session from the relation store.

        Does not raise when *session_id* is unknown.
        """
        self._ensure_loaded()
        parent = self._parents.pop(session_id, None)
        self._created_at.pop(session_id, None)
        if parent is not None and parent in self._children:
            self._children[parent] = [
                c for c in self._children[parent] if c != session_id
            ]
        # Persist removal — rewrite the relations file for the session's pool
        self._remove_from_persisted(session_id)

    def delete_conversation(self, conv_id: str) -> None:
        """Delete all child relations belonging to *conv_id*.

        Relations from other conversations are left untouched.
        """
        self._ensure_loaded()
        # Collect all session_ids in this conversation (children + parents)
        to_remove: set[str] = set()
        for child, parent in list(self._parents.items()):
            if _conv_of(child) == conv_id:
                to_remove.add(child)
        # Also remove entries where this conv's sessions are parents
        for child, parent in list(self._parents.items()):
            if _conv_of(parent) == conv_id:
                to_remove.add(child)

        for sid in to_remove:
            self._parents.pop(sid, None)
            self._created_at.pop(sid, None)

        # Rebuild children index for affected parents
        for parent_key in list(self._children.keys()):
            if _conv_of(parent_key) == conv_id:
                self._children.pop(parent_key, None)
            else:
                self._children[parent_key] = [
                    c for c in self._children[parent_key] if c not in to_remove
                ]

        # Persist: rewrite all affected _relations.json files
        self._persist_all()

    def list_all(self) -> dict[str, str]:
        """Return all child→parent mappings (in-memory)."""
        self._ensure_loaded()
        return dict(self._parents)

    # ------------------------------------------------------------------
    # Derivation fallback
    # ------------------------------------------------------------------

    def _derive_parent(self, session_id: str) -> str | None:
        """Derive parent from session_id format when no persisted record exists.

        Derivation only applies to agents **not** in the agent_pool_map
        (dynamic subagents).  Agents that ARE in the map — both main agents
        and resident subagents — return None when no persisted record exists.

        Logic:
        1. Parse session_id → conv, agent
        2. If agent IS in the agent_pool_map → None (known agent, no fallback)
        3. If agent is NOT in the map → prefix-match to find pool → ``{conv}.{main_agent}``
        4. If no pool info → None
        """
        conv = _conv_of(session_id)
        agent = _agent_of(session_id)

        if not self._agent_pool_map:
            return None

        # Only derive for agents NOT in the map (dynamic subagents).
        # Agents in the map are known and must have set_parent() called explicitly.
        if agent in self._agent_pool_map:
            return None

        # Try prefix matching for dynamic subagents (e.g., "scout-a1b2c3" matches "scout")
        pool = self._resolve_pool(agent)
        if pool is None:
            return None

        # Find main agent for this pool (the one where agent_name == pool_name)
        main_agent = self._main_agent_for_pool(pool)
        if main_agent is None:
            return None

        return f"{conv}.{main_agent}"

    def _resolve_pool(self, agent: str) -> str | None:
        """Resolve the pool name for *agent*.

        Checks: exact match in map → prefix match (for dynamic subagents).
        """
        if agent in self._agent_pool_map:
            return self._agent_pool_map[agent]
        # Dynamic subagent: "{template_type}-{invocation_id}"
        for tmpl_type, pool in self._agent_pool_map.items():
            if agent.startswith(f"{tmpl_type}-"):
                return pool
        return None

    def _main_agent_for_pool(self, pool: str) -> str | None:
        """Return the main agent name for *pool*.

        The main agent is the agent whose name equals the pool name.
        """
        if pool in self._agent_pool_map and self._agent_pool_map[pool] == pool:
            return pool
        # Fallback: scan for an agent whose name matches the pool
        for agent, p in self._agent_pool_map.items():
            if p == pool and agent == pool:
                return agent
        return None

    # ------------------------------------------------------------------
    # Loading & persistence
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load all _relations.json files from disk (once).

        Scans every ``<base>/<pool>/_relations.json`` and merges into
        the in-memory cache.
        """
        if self._loaded:
            return
        self._loaded = True
        if not self._base.is_dir():
            return
        for pool_dir in sorted(self._base.iterdir()):
            if not pool_dir.is_dir():
                continue
            rel_path = pool_dir / _RELATIONS_FILENAME
            data = _read_json(rel_path)
            for child_sid, entry in data.items():
                parent = entry.get("parent_session_id")
                created = entry.get("created_at")
                if isinstance(parent, str) and isinstance(created, (int, float)):
                    self._parents[child_sid] = parent
                    self._created_at[child_sid] = int(created)
                    if parent not in self._children:
                        self._children[parent] = []
                    if child_sid not in self._children[parent]:
                        self._children[parent].append(child_sid)
        # Sort all children by created_at
        for parent in self._children:
            self._children[parent].sort(
                key=lambda sid: self._created_at.get(sid, 0)
            )

    def _relations_path_for(self, session_id: str) -> Path | None:
        """Return the path to ``_relations.json`` for the pool owning *session_id*.

        Path: ``<base>/<pool>/_relations.json``.  The base directory already
        encodes the workspace.
        """
        agent = _agent_of(session_id)
        pool = self._resolve_pool(agent) or _DEFAULT_POOL
        return self._base / pool / _RELATIONS_FILENAME

    def _persist_relations_for(self, child_session_id: str) -> None:
        """Write the in-memory state to the ``_relations.json`` for the child's pool."""
        path = self._relations_path_for(child_session_id)
        if path is None:
            return
        # Collect all relations for this pool
        pool_data: dict[str, dict[str, object]] = {}
        pool_dir = path.parent
        for child_sid, parent_sid in self._parents.items():
            candidate = self._relations_path_for(child_sid)
            if candidate is not None and candidate.parent == pool_dir:
                pool_data[child_sid] = {
                    "parent_session_id": parent_sid,
                    "created_at": self._created_at.get(child_sid, 0),
                }
        _write_atomic(path, pool_data)

    def _remove_from_persisted(self, session_id: str) -> None:
        """Rewrite the relations file for *session_id*'s pool after removal."""
        path = self._relations_path_for(session_id)
        if path is None:
            return
        data = _read_json(path)
        data.pop(session_id, None)
        _write_atomic(path, data)

    def _persist_all(self) -> None:
        """Rewrite all _relations.json files from in-memory state."""
        # Group by pool directory
        pool_groups: dict[Path, dict[str, dict[str, object]]] = {}
        for child_sid, parent_sid in self._parents.items():
            path = self._relations_path_for(child_sid)
            if path is None:
                continue
            pool_dir = path.parent
            if pool_dir not in pool_groups:
                pool_groups[pool_dir] = {}
            pool_groups[pool_dir][child_sid] = {
                "parent_session_id": parent_sid,
                "created_at": self._created_at.get(child_sid, 0),
            }
        for pool_dir, data in pool_groups.items():
            _write_atomic(pool_dir / _RELATIONS_FILENAME, data)
