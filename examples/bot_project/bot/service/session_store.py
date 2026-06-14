"""Session storage partitioned by workspace and pool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from framework.core.session_id import SessionId
from framework.core.session_store import LocalFileSessionStore


def _safe_name(name: str) -> str:
    """Replace characters unsafe for file names across platforms."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


class WorkspacePoolSessionStore(LocalFileSessionStore):
    """Session store partitioned into ``<workspace>/<pool>/`` subdirectories.

    The workspace and pool are resolved at write time via callables,
    so a session's location reflects the current workspace context.
    """

    def __init__(
        self,
        base_dir: Path,
        workspace_resolver: Callable[[], str],
        pool_resolver: Callable[[SessionId], str],
    ) -> None:
        super().__init__(base_dir)
        self._workspace_resolver = workspace_resolver
        self._pool_resolver = pool_resolver

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        """Resolve path for *session_id* by scanning subdirectories.

        Returns the first matching file, or a fallback path under the
        root if not found (used by delete).
        """
        safe = _safe_name(session_id)
        filename = f"{safe}.json"
        for json_file in self._root.glob(f"**/{filename}"):
            return json_file
        return self._root / filename

    def _scan_json_files(self) -> list[Path]:
        return sorted(self._root.glob("**/*.json"))

    def _read_session(self, path: Path) -> SessionId:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionId(**data)

    # ------------------------------------------------------------------
    # SessionStore interface
    # ------------------------------------------------------------------

    async def save(self, session: SessionId) -> None:
        workspace = self._workspace_resolver()
        pool = self._pool_resolver(session)
        target_dir = self._root / workspace / pool
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{_safe_name(str(session))}.json"
        path.write_text(session.model_dump_json(), encoding="utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        safe = _safe_name(session_id)
        filename = f"{safe}.json"
        for json_file in self._root.glob(f"**/{filename}"):
            return self._read_session(json_file)
        return None

    async def delete(self, session_id: str) -> None:
        safe = _safe_name(session_id)
        filename = f"{safe}.json"
        for json_file in self._root.glob(f"**/{filename}"):
            json_file.unlink()
            # Optionally clean up empty parent directories
            try:
                parent = json_file.parent
                while parent != self._root:
                    if not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                    else:
                        break
            except OSError:
                pass
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
