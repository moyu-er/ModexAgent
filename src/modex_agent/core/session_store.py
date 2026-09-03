"""SessionStore — persistent session storage.

The store is the authoritative source of session data. Each session is stored
as a JSON file keyed by its ``session_id`` string.

I/O is dispatched via ``asyncio.to_thread`` so disk operations never block the
event loop.
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.core.session_id import SessionInfo
from modex_agent.utils.file_io import atomic_write_text


def safe_filename(name: str) -> str:
    """Replace characters unsafe for file names across platforms.

    All session stores and transcript stores must use this single implementation
    so session_id → filename mapping is consistent.
    """
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


class SessionStore(ABC):
    """Persistent storage for SessionInfo records.

    The store is constructed with a root path (see :class:`LocalFileSessionStore`
    and subclasses). Workspace-aware callers construct a fresh store per
    workspace — the root IS the workspace's session index directory — rather
    than passing a per-call override. In-turn writers (e.g.
    :class:`~modex_agent.core.session_registry.InMemorySessionRegistry`) may
    still honour a bound workspace-root contextvar inside a dispatch turn.
    """

    @abstractmethod
    async def save(self, session: SessionInfo) -> None:
        """Persist a session record (create or update)."""
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionInfo | None:
        """Retrieve a session by id, or None if not found."""
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove a session record."""
        ...

    @abstractmethod
    async def list_sessions(self) -> list[SessionInfo]:
        """Return all stored sessions."""
        ...

    @abstractmethod
    async def get_children(self, parent_id: str) -> list[SessionInfo]:
        """Return sessions whose ``parent_session_id`` matches *parent_id*."""
        ...


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
        ``<root>/<pool>/<id>.json``).  When no existing record is found, fall
        back to the flat ``<root>/<id>.json`` path for new writes.
        """
        base = self._root
        safe = safe_filename(session_id)
        filename = f"{safe}.json"
        for f in base.glob(f"**/{filename}"):
            return f
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

        def _rm() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_rm)

    async def list_sessions(self) -> list[SessionInfo]:
        base = self._root

        def _collect() -> list[str]:
            results: list[str] = []
            for f in sorted(base.glob("**/*.json")):
                results.append(f.read_text(encoding="utf-8"))
            return results

        texts = await asyncio.to_thread(_collect)
        return [SessionInfo(**json.loads(t)) for t in texts]

    async def get_children(self, parent_id: str) -> list[SessionInfo]:
        results: list[SessionInfo] = []
        for session in await self.list_sessions():
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
