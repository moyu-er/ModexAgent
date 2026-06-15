"""SessionInfo index partitioned by pool — ``<root>/<pool>/{safe_id}.json``.

Workspace isolation is handled externally by rebasing ``_root`` when the
workspace changes.  Pool is resolved at write time via a callable so each
session lands in the correct pool directory, consistent with
``memory/<pool>/`` and ``sessions/<pool>/``.

I/O is dispatched via ``asyncio.to_thread`` so disk operations never block the
event loop.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from collections.abc import Callable

from framework.core.session_id import SessionInfo
from framework.core.session_store import LocalFileSessionStore, safe_filename


class WorkspacePoolSessionStore(LocalFileSessionStore):
    """Session index with pool subdirectory layering.

    *save* writes to ``<root>/<pool>/<safe_id>.json``.
    *get* / *delete* find the existing record via ``_path_for`` (glob-based,
    backward-compatible with a flat root or records moved between pools).
    """

    def __init__(self, base_dir: Path, pool_resolver: Callable[[SessionInfo], str]) -> None:
        super().__init__(base_dir)
        self._pool_resolver = pool_resolver

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        """Find existing record in any pool subdirectory; fallback to root."""
        safe = safe_filename(session_id)
        filename = f"{safe}.json"
        for json_file in self._root.glob(f"**/{filename}"):
            return json_file
        return self._root / filename

    @staticmethod
    def _read_json(path: Path) -> SessionInfo:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionInfo(**data)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def save(self, session: SessionInfo) -> None:
        pool = self._pool_resolver(session)
        target_dir = self._root / pool
        path = target_dir / f"{safe_filename(str(session))}.json"

        def _write() -> None:
            target_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(session.model_dump_json(), encoding="utf-8")

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

    async def list_sessions(self) -> list[SessionInfo]:

        def _collect() -> list[str]:
            results: list[str] = []
            for f in sorted(self._root.glob("**/*.json")):
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

    async def get_children(self, parent_id: str) -> list[SessionInfo]:
        results: list[SessionInfo] = []
        for session in await self.list_sessions():
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
