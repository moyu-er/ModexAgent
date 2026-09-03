"""SessionStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`LocalFileSessionStore`.
SQLite: :class:`SqliteSessionStore` (over ``ConnectionManager``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.persistence.adapters.session_store import SqliteSessionStore
from modex_agent.persistence.session_store import SessionStore


def _session(
    sid: str = "s1",
    agent: str = "main",
    parent: str | None = None,
) -> SessionInfo:
    return SessionInfo(session_id=sid, agent_name=agent, parent_session_id=parent)


@pytest.fixture(params=["file", "sqlite"])
async def session_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncGenerator[SessionStore]:
    """Parametrized SessionStore — file (LocalFileSessionStore) or sqlite."""
    if request.param == "file":
        yield LocalFileSessionStore(tmp_path / "sessions_file")
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteSessionStore(mgr)
        await mgr.close()


class TestSessionStoreConformance:
    """Same behavior on both backends."""

    async def test_get_missing_returns_none(self, session_store: SessionStore) -> None:
        assert await session_store.get("nope") is None

    async def test_save_then_get_roundtrip(self, session_store: SessionStore) -> None:
        s = _session("s1", "main")
        await session_store.save(s)
        got = await session_store.get("s1")
        assert got is not None
        assert got.session_id == "s1"
        assert got.agent_name == "main"

    async def test_save_upsert_updates_existing(self, session_store: SessionStore) -> None:
        await session_store.save(_session("s1", "main"))
        await session_store.save(_session("s1", "other"))
        got = await session_store.get("s1")
        assert got is not None
        assert got.agent_name == "other"

    async def test_delete_removes_session(self, session_store: SessionStore) -> None:
        await session_store.save(_session("s1"))
        await session_store.delete("s1")
        assert await session_store.get("s1") is None

    async def test_delete_missing_is_noop(self, session_store: SessionStore) -> None:
        await session_store.delete("nope")  # must not raise

    async def test_list_sessions_returns_all(self, session_store: SessionStore) -> None:
        await session_store.save(_session("s1"))
        await session_store.save(_session("s2"))
        sessions = await session_store.list_sessions()
        ids = {s.session_id for s in sessions}
        assert ids == {"s1", "s2"}

    async def test_get_children_returns_descendants(self, session_store: SessionStore) -> None:
        await session_store.save(_session("parent", "main"))
        await session_store.save(_session("parent.sub1", "sub", parent="parent"))
        await session_store.save(_session("parent.sub2", "sub", parent="parent"))
        await session_store.save(_session("orphan", "main"))
        children = await session_store.get_children("parent")
        child_ids = {c.session_id for c in children}
        assert child_ids == {"parent.sub1", "parent.sub2"}

    async def test_get_children_no_children_returns_empty(
        self, session_store: SessionStore
    ) -> None:
        await session_store.save(_session("s1"))
        children = await session_store.get_children("s1")
        assert children == []
