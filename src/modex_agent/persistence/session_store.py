"""Persistent session storage contract and shared filename mapping."""

from __future__ import annotations

from abc import ABC, abstractmethod

from modex_agent.core.session_id import SessionInfo


def safe_filename(name: str) -> str:
    """Replace characters unsafe for file names across platforms.

    All session stores and transcript stores must use this single implementation
    so session_id -> filename mapping is consistent.
    """
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name


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
