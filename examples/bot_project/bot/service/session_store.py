"""SessionId index partitioned by pool — ``<root>/<pool>/{safe_id}.json``.

Workspace isolation is handled externally by rebasing ``_root`` when the
workspace changes.  Pool is resolved at write time via a callable so each
session lands in the correct pool directory, consistent with
``memory/<pool>/`` and ``sessions/<pool>/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Callable

from framework.core.session_id import SessionId
from framework.core.session_store import LocalFileSessionStore


def _safe_name(name: str) -> str:
    """Replace characters unsafe for file names across platforms."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


class WorkspacePoolSessionStore(LocalFileSessionStore):
    """Session index with pool subdirectory layering.

    *save* writes to ``<root>/<pool>/<safe_id>.json``.
    *get* scans subdirectories via glob (backward-compatible with a flat root).
    """

    def __init__(self, base_dir: Path, pool_resolver: Callable[[SessionId], str]) -> None:
        super().__init__(base_dir)
        self._pool_resolver = pool_resolver

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _scan_json_files(self) -> list[Path]:
        return sorted(self._root.glob("**/*.json"))

    def _read_session(self, path: Path) -> SessionId:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionId(**data)

    def _path_for(self, session_id: str) -> Path:
        """Find existing record in any pool subdirectory; fallback to root."""
        safe = _safe_name(session_id)
        filename = f"{safe}.json"
        for json_file in self._root.glob(f"**/{filename}"):
            return json_file
        return self._root / filename

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def save(self, session: SessionId) -> None:
        pool = self._pool_resolver(session)
        target_dir = self._root / pool
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{_safe_name(str(session))}.json"
        path.write_text(session.model_dump_json(), encoding="utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        for json_file in self._root.glob(f"**/{_safe_name(session_id)}.json"):
            return self._read_session(json_file)
        return None

    async def delete(self, session_id: str) -> None:
        for json_file in self._root.glob(f"**/{_safe_name(session_id)}.json"):
            json_file.unlink()
            return

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
