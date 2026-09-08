"""File-system-backed session storage adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence.session_store import SessionStore, safe_filename
from modex_agent.utils.file_io import atomic_write_text


class LocalFileSessionStore(SessionStore):
    """File-system backed session store (one JSON file per session)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def rebase(self, new_root: Path) -> None:
        """Point the store at a new root directory (workspace switch)."""
        self._root = Path(new_root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, session_id: str) -> Path:
        """Return the path for *session_id*, finding existing records recursively.

        Session records may live in pool subdirectories (e.g.
        ``<root>/<pool>/<id>.json``). When no existing record is found, fall
        back to the flat ``<root>/<id>.json`` path for new writes.
        """
        base = self._root
        safe = safe_filename(session_id)
        filename = f"{safe}.json"
        for file_path in base.glob(f"**/{filename}"):
            return file_path
        return base / filename

    async def save(self, session: SessionInfo) -> None:
        path = self._path_for(session.session_id)
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

        def _remove() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_remove)

    async def list_sessions(self) -> list[SessionInfo]:
        base = self._root

        def _collect() -> list[str]:
            results: list[str] = []
            for file_path in sorted(base.glob("**/*.json")):
                results.append(file_path.read_text(encoding="utf-8"))
            return results

        texts = await asyncio.to_thread(_collect)
        return [SessionInfo(**json.loads(text)) for text in texts]

    async def get_children(self, parent_id: str) -> list[SessionInfo]:
        results: list[SessionInfo] = []
        for session in await self.list_sessions():
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
