"""SessionStore — persistent session storage.

The store is the authoritative source of session data. Each session is stored
as a JSON file keyed by its ``session_id`` string.

I/O is dispatched via ``asyncio.to_thread`` so disk operations never block the
event loop.
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path

from framework.core.session_id import SessionInfo


def safe_filename(name: str) -> str:
    """Replace characters unsafe for file names across platforms.

    All session stores and transcript stores must use this single implementation
    so session_id → filename mapping is consistent.
    """
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically via a temp file + ``os.replace``.

    The target is never observed in a partially-written state: either the
    previous content remains (if the final replace fails) or the new content
    is fully in place.  The temp file is cleaned up on failure.
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class SessionStore(ABC):
    """Persistent storage for SessionInfo records.

    Every method takes an optional ``index_dir`` override. Workspace-aware
    implementations (e.g. :class:`bot.service.session_store.WorkspacePoolSessionStore`)
    route the I/O to *index_dir* when given, so HTTP/WS handlers can read/write
    a specific workspace's session index. The default (``None``) is
    implementation-defined (typically the configured home root).
    """

    @abstractmethod
    async def save(self, session: SessionInfo, index_dir: Path | None = None) -> None:
        """Persist a session record (create or update)."""
        ...

    @abstractmethod
    async def get(self, session_id: str, index_dir: Path | None = None) -> SessionInfo | None:
        """Retrieve a session by id, or None if not found."""
        ...

    @abstractmethod
    async def delete(self, session_id: str, index_dir: Path | None = None) -> None:
        """Remove a session record."""
        ...

    @abstractmethod
    async def list_sessions(self, index_dir: Path | None = None) -> list[SessionInfo]:
        """Return all stored sessions."""
        ...

    @abstractmethod
    async def get_children(
        self, parent_id: str, index_dir: Path | None = None
    ) -> list[SessionInfo]:
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

    def _path_for(self, session_id: str, root: Path | None = None) -> Path:
        """Return the path for *session_id*, finding existing records recursively.

        Session records may live in pool subdirectories (e.g.
        ``<root>/<pool>/<id>.json``).  When no existing record is found, fall
        back to the flat ``<root>/<id>.json`` path for new writes.
        """
        base = root if root is not None else self._root
        safe = safe_filename(session_id)
        filename = f"{safe}.json"
        for f in base.glob(f"**/{filename}"):
            return f
        return base / filename

    async def save(self, session: SessionInfo, index_dir: Path | None = None) -> None:
        path = self._path_for(session.session_id, index_dir)
        payload = session.model_dump_json()

        def _write() -> None:
            atomic_write_text(path, payload)

        await asyncio.to_thread(_write)

    async def get(self, session_id: str, index_dir: Path | None = None) -> SessionInfo | None:
        path = self._path_for(session_id, index_dir)

        def _read() -> str | None:
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")

        text = await asyncio.to_thread(_read)
        if text is None:
            return None
        return SessionInfo(**json.loads(text))

    async def delete(self, session_id: str, index_dir: Path | None = None) -> None:
        path = self._path_for(session_id, index_dir)

        def _rm() -> None:
            if path.exists():
                path.unlink()

        await asyncio.to_thread(_rm)

    async def list_sessions(self, index_dir: Path | None = None) -> list[SessionInfo]:
        base = index_dir if index_dir is not None else self._root

        def _collect() -> list[str]:
            results: list[str] = []
            for f in sorted(base.glob("**/*.json")):
                results.append(f.read_text(encoding="utf-8"))
            return results

        texts = await asyncio.to_thread(_collect)
        return [SessionInfo(**json.loads(t)) for t in texts]

    async def get_children(
        self, parent_id: str, index_dir: Path | None = None
    ) -> list[SessionInfo]:
        results: list[SessionInfo] = []
        for session in await self.list_sessions(index_dir):
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
