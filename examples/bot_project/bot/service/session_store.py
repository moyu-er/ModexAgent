"""SessionInfo index partitioned by pool — ``<index_dir>/<pool>/{safe_id}.json``.

Workspace isolation is driven by the ``index_dir`` override on every method:
each workspace owns its own ``session_index`` directory, so sessions created
under a workspace never leak into another workspace's listing. HTTP/WS handlers
resolve the per-workspace ``index_dir`` (from the request's ``?ws=``) and pass
it in; callers that omit it fall back to ``base_dir`` (the configured home
root). Pool is resolved at write time via a callable so each session lands in
the correct pool directory, consistent with ``memory/<pool>/`` and
``sessions/<pool>/``.

I/O is dispatched via ``asyncio.to_thread`` so disk operations never block the
event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from collections.abc import Callable

from framework.core.session_id import SessionInfo, session_id_prefix_of

logger = logging.getLogger(__name__)
from framework.core.session_store import LocalFileSessionStore, atomic_write_text, safe_filename


class WorkspacePoolSessionStore(LocalFileSessionStore):
    """Session index with pool subdirectory layering.

    *save* writes to ``<index_dir>/<pool>/<safe_id>.json`` (``base_dir`` when no
    ``index_dir`` is given). *get* / *delete* find the existing record via
    ``_path_for`` (glob-based under the resolved root, backward-compatible with
    a flat root or records moved between pools).
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

    def _root_for(self, index_dir: Path | None) -> Path:
        """The root to read/write under.

        - *index_dir* when given (explicit ws scope, e.g. HTTP handlers).
        - The bound workspace root's session-index dir when inside a dispatch
          turn (``InMemorySessionRegistry`` calls, e.g. the PoolRouter touching
          or registering a session).
        - ``base_dir`` (home) otherwise — backward compat for tests and
          non-dispatch callers.
        """
        if index_dir is not None:
            return index_dir
        from framework.workspace.runtime import (
            is_workspace_root_bound,
            resolve_workspace_root,
        )
        if is_workspace_root_bound():
            from framework.workspace.paths import WorkspacePaths
            root = resolve_workspace_root()
            return WorkspacePaths(
                root=root / self._data_dir_name
            ).session_index_dir
        return self._root

    def _path_for(self, session_id: str, root: Path | None = None) -> Path:
        """Find existing record in any pool subdirectory; fallback to root."""
        base = root if root is not None else self._root
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

    async def save(self, session: SessionInfo, index_dir: Path | None = None) -> None:
        # Convergence guard: the owning workspace is resolved from the bound
        # root ctxvar when no explicit index_dir is given. A save with neither
        # silently lands in base_dir (home) — almost always a bug (an out-of-turn
        # registration). Surface it loudly instead of leaking the record into home.
        if index_dir is None:
            from framework.workspace.runtime import is_workspace_root_bound

            if not is_workspace_root_bound():
                logger.warning(
                    "[ws-partition] session-index save for %s with NO explicit "
                    "index_dir and NO bound workspace root — writing under base_dir "
                    "(home). This is expected only outside a turn; a turn-time "
                    "writer must run inside bind_workspace_root().",
                    session.session_id,
                )
        root = self._root_for(index_dir)
        pool = self._pool_resolver(session)
        path = root / pool / f"{safe_filename(session.session_id)}.json"
        payload = session.model_dump_json()

        def _write() -> None:
            atomic_write_text(path, payload)

        await asyncio.to_thread(_write)

    async def get(self, session_id: str, index_dir: Path | None = None) -> SessionInfo | None:
        path = self._path_for(session_id, self._root_for(index_dir))

        def _read() -> str | None:
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")

        text = await asyncio.to_thread(_read)
        if text is None:
            return None
        return SessionInfo(**json.loads(text))

    async def delete(self, session_id: str, index_dir: Path | None = None) -> None:
        path = self._path_for(session_id, self._root_for(index_dir))

        def _rm() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_rm)

    async def delete_sessions_by_prefix(
        self, session_prefix: str, index_dir: Path | None = None
    ) -> None:
        """Remove every record whose session id shares *session_prefix*.

        A conversation (prefix) owns the main session plus every subagent
        invocation session; deleting the conversation must sweep all of them so
        subagent invocation index files don't accumulate as orphans. Mirrors
        :meth:`TranscriptStore.delete_sessions_by_prefix`.
        """
        root = self._root_for(index_dir)
        for session in await self.list_sessions(index_dir):
            if session_id_prefix_of(session.session_id) == session_prefix:
                await self.delete(session.session_id, root)

    async def list_sessions(self, index_dir: Path | None = None) -> list[SessionInfo]:
        base = self._root_for(index_dir)

        def _collect() -> list[str]:
            results: list[str] = []
            for f in sorted(base.glob("**/*.json")):
                results.append(f.read_text(encoding="utf-8"))
            return results

        texts = await asyncio.to_thread(_collect)
        sessions: list[SessionInfo] = []
        for t in texts:
            try:
                sessions.append(SessionInfo(**json.loads(t)))
            except Exception:
                pass
        return sessions

    async def get_children(
        self, parent_id: str, index_dir: Path | None = None
    ) -> list[SessionInfo]:
        results: list[SessionInfo] = []
        for session in await self.list_sessions(index_dir):
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
