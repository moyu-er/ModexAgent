"""Persistent session storage contract and shared filename mapping."""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.core.session_id import SessionInfo
from modex_agent.utils.file_io import safe_filename

__all__ = ["SessionStore", "safe_filename"]


class SessionStore(ABC):
    """Persistent storage for SessionInfo records.

    Workspace-aware callers construct a fresh store per workspace rather than
    passing a per-call override. In-turn writers may still honour a bound
    workspace-root context variable inside a dispatch turn.
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
