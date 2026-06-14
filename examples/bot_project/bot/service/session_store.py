"""Flat SessionId index store — one JSON file per session, no layering.

The store is workspace/pool agnostic.  Workspace switching is handled
externally by rebasing ``_root``.  Discovery uses a single-level glob on
``<root>/{safe_id}.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from framework.core.session_id import SessionId
from framework.core.session_store import LocalFileSessionStore


def _safe_name(name: str) -> str:
    """Replace characters unsafe for file names across platforms."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


class WorkspacePoolSessionStore(LocalFileSessionStore):
    """Flat SessionId index — ``<root>/{safe_id}.json``.

    Workspace isolation is managed externally: the consumer rebases
    ``_root`` when the workspace changes.  No path-level layering is
    needed because ``session_id`` is globally unique.
    """

    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        return self._root / f"{_safe_name(session_id)}.json"

    def _scan_json_files(self) -> list[Path]:
        return sorted(self._root.glob("*.json"))

    def _read_session(self, path: Path) -> SessionId:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionId(**data)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def save(self, session: SessionId) -> None:
        path = self._path_for(str(session))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session.model_dump_json(), encoding="utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        path = self._path_for(session_id)
        if not path.exists():
            return None
        return self._read_session(path)

    async def delete(self, session_id: str) -> None:
        path = self._path_for(session_id)
        if path.exists():
            path.unlink()

    async def list_sessions(self) -> list[SessionId]:
        results: list[SessionId] = []
        for f in self._scan_json_files():
            results.append(self._read_session(f))
        return results

    async def get_children(self, parent_id: str) -> list[SessionId]:
        results: list[SessionId] = []
        for session in await self.list_sessions():
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
