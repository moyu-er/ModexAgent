"""SessionStore — persistent session storage.

The store is the authoritative source of session data. Each session is stored
as a JSON file keyed by its ``session_id`` string.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from framework.core.session_id import SessionId


class SessionStore(ABC):
    """Persistent storage for SessionId records."""

    @abstractmethod
    async def save(self, session: SessionId) -> None:
        """Persist a session record (create or update)."""
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None:
        """Retrieve a session by id, or None if not found."""
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """Remove a session record."""
        ...

    @abstractmethod
    async def list_sessions(self) -> list[SessionId]:
        """Return all stored sessions."""
        ...

    @abstractmethod
    async def get_children(self, parent_id: str) -> list[SessionId]:
        """Return sessions whose ``parent_session_id`` matches *parent_id*."""
        ...


class LocalFileSessionStore(SessionStore):
    """File-system backed session store (one JSON file per session)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"

    async def save(self, session: SessionId) -> None:
        path = self._path_for(str(session))
        path.write_text(session.model_dump_json(), encoding="utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        path = self._path_for(session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionId(**data)

    async def delete(self, session_id: str) -> None:
        path = self._path_for(session_id)
        if path.exists():
            path.unlink()

    async def list_sessions(self) -> list[SessionId]:
        results: list[SessionId] = []
        for f in sorted(self._root.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            results.append(SessionId(**data))
        return results

    async def get_children(self, parent_id: str) -> list[SessionId]:
        results: list[SessionId] = []
        for session in await self.list_sessions():
            if session.parent_session_id == parent_id:
                results.append(session)
        return results
