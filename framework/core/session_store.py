"""SessionStore — authoritative persistence for SessionId records.

Framework provides the ABC and a flat-file default. Business inherits
LocalFileSessionStore to add workspace/pool directory partitioning.
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from pathlib import Path

from framework.core.session_id import SessionId


def _safe_name(session_id: str) -> str:
    """Replace path-unsafe characters for filesystem use."""
    return re.sub(r"[^\w\-.]", "_", session_id)


class SessionStore(ABC):
    """Authoritative persistent store of SessionId records.

    No workspace/pool awareness — that is the business layer's concern.
    """

    @abstractmethod
    async def save(self, session: SessionId) -> None:
        ...

    @abstractmethod
    async def get(self, session_id: str) -> SessionId | None:
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        ...

    @abstractmethod
    async def list_sessions(self) -> list[SessionId]:
        ...

    @abstractmethod
    async def get_children(self, parent_session_id: str) -> list[SessionId]:
        ...


class LocalFileSessionStore(SessionStore):
    """Flat-file store: one JSON file per session, keyed by safe session_id.

    Layout: ``<base_dir>/<safe_session_id>.json``. Business subclasses override
    ``_path_for`` to add workspace/pool subdirectories.
    """

    def __init__(self, base_dir: Path) -> None:
        self._base = Path(base_dir)

    def _path_for(self, session_id: str) -> Path:
        return self._base / f"{_safe_name(session_id)}.json"

    async def save(self, session: SessionId) -> None:
        path = self._path_for(str(session))
        await asyncio.to_thread(self._write, path, session.model_dump_json())

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    async def get(self, session_id: str) -> SessionId | None:
        path = self._path_for(session_id)
        if not await asyncio.to_thread(path.is_file):
            return None
        text = await asyncio.to_thread(path.read_text, "utf-8")
        return SessionId.model_validate_json(text)

    async def delete(self, session_id: str) -> None:
        path = self._path_for(session_id)
        await asyncio.to_thread(path.unlink, True)

    async def list_sessions(self) -> list[SessionId]:
        paths = await asyncio.to_thread(self._collect_paths)
        sessions: list[SessionId] = []
        for path in paths:
            text = await asyncio.to_thread(path.read_text, "utf-8")
            sessions.append(SessionId.model_validate_json(text))
        return sessions

    def _collect_paths(self) -> list[Path]:
        if not self._base.is_dir():
            return []
        return [p for p in self._base.glob("*.json") if p.is_file()]

    async def get_children(self, parent_session_id: str) -> list[SessionId]:
        all_sessions = await self.list_sessions()
        return [s for s in all_sessions if s.parent_session_id == parent_session_id]
