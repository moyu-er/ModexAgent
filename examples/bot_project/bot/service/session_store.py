"""SessionInfo index partitioned by pool — ``<root>/<pool>/{safe_id}.json``.

Workspace isolation is driven by store construction: each workspace owns its
own ``session_index`` directory, so the store is constructed with that
directory as ``base_dir``. The WebUI server builds a fresh store per
workspace via a factory; in-turn writers (``InMemorySessionRegistry``) use a
per-workspace store constructed in ``wiring._build_resources``. A bound
workspace-root contextvar (inside a dispatch turn) still overrides the root
so the shared registry routes writes to the active workspace. Pool is
resolved at write time via a callable so each session lands in the correct
pool directory, consistent with ``memory/<pool>/`` and ``sessions/<pool>/``.

I/O is dispatched via ``asyncio.to_thread`` so disk operations never block the
event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from pathlib import Path

from modex_agent.core.session_id import SessionInfo, session_id_prefix_of
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.session_store import safe_filename
from modex_agent.utils.file_io import atomic_write_text


class WorkspacePoolSessionStore(LocalFileSessionStore):
    """Session index with pool subdirectory layering.

    *save* writes to ``<root>/<pool>/<safe_id>.json`` where ``root`` is the
    ``base_dir`` passed at construction (or the bound workspace root's
    session-index dir when inside a dispatch turn). *get* / *delete* find the
    existing record via ``_path_for`` (glob-based under the resolved root,
    backward-compatible with a flat root or records moved between pools).
    """

    def __init__(
        self,
        base_dir: Path,
        pool_resolver: Callable[[SessionInfo], str],
        data_dir_name: str = ".modex",
    ) -> None:
        super().__init__(base_dir)
        self._pool_resolver = pool_resolver
        self._data_dir_name: str = data_dir_name

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _root_for(self) -> Path:
        """The root to read/write under.

        - The bound workspace root's session-index dir when inside a dispatch
          turn (``InMemorySessionRegistry`` calls, e.g. the PoolRouter touching
          or registering a session).
        - ``self._root`` (the construction-time ``base_dir``) otherwise — used
          by HTTP/WS handlers that build a fresh store per workspace.
        """
        from modex_agent.workspace.runtime import (
            is_workspace_root_bound,
            resolve_workspace_root,
        )

        if is_workspace_root_bound():
            from modex_agent.workspace.paths import WorkspacePaths

            root = resolve_workspace_root()
            return WorkspacePaths(root=root / self._data_dir_name).session_index_dir
        return self._root

    def _path_for(self, session_id: str) -> Path:
        """Find existing record in any pool subdirectory; fallback to root."""
        base = self._root_for()
        safe = safe_filename(session_id)
        filename = f"{safe}.json"
        for json_file in base.glob(f"**/{filename}"):
            return json_file
        return base / filename

    @staticmethod
    def _read_json(path: Path) -> SessionInfo:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionInfo(**data)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def save(self, session: SessionInfo) -> None:
        root = self._root_for()
        pool = self._pool_resolver(session)
        path = root / pool / f"{safe_filename(session.session_id)}.json"
        payload = session.model_dump_json()

        def _write() -> None:
            atomic_write_text(path, payload)

        await asyncio.to_thread(_write)

    async def get(self, session_id: str) -> SessionInfo | None:
        path = self._path_for(session_id)

        def _read() -> str | None:
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")

        text = await asyncio.to_thread(_read)
        if text is None:
            return None
        return SessionInfo(**json.loads(text))

    async def delete(self, session_id: str) -> None:
        path = self._path_for(session_id)

        def _rm() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_rm)

    async def delete_sessions_by_prefix(self, session_prefix: str) -> None:
        """Remove every record whose session id shares *session_prefix*.

        A conversation (prefix) owns the main session plus every subagent
        invocation session; deleting the conversation must sweep all of them so
        subagent invocation index files don't accumulate as orphans. Mirrors
        :meth:`TranscriptStore.delete_sessions_by_prefix`.
        """
        for session in await self.list_sessions():
            if session_id_prefix_of(session.session_id) == session_prefix:
                await self.delete(session.session_id)

    async def list_sessions(self) -> list[SessionInfo]:
        base = self._root_for()

        def _collect() -> list[str]:
            results: list[str] = []
            for f in sorted(base.glob("**/*.json")):
                results.append(f.read_text(encoding="utf-8"))
            return results

        texts = await asyncio.to_thread(_collect)
        sessions: list[SessionInfo] = []
        for t in texts:
            with contextlib.suppress(Exception):
                sessions.append(SessionInfo(**json.loads(t)))
        return sessions

    async def get_children(self, parent_id: str) -> list[SessionInfo]:
        results: list[SessionInfo] = []
        for session in await self.list_sessions():
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
