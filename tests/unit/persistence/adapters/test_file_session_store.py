"""Tests for LocalFileSessionStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_save_and_get_roundtrip(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main", metadata={"pool": "coding"})
    await store.save(session)
    got = await store.get(session.session_id)
    assert got is not None
    assert got == session
    assert got.metadata == {"pool": "coding"}


async def test_get_missing_returns_none(tmp_path: Path):
    store = LocalFileSessionStore(tmp_path)
    assert await store.get("nope.main") is None


async def test_delete_removes_session(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main")
    await store.save(session)
    await store.delete(session.session_id)
    assert await store.get(session.session_id) is None


async def test_list_sessions_returns_all(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    a = factory.create(agent_name="main")
    b = factory.create(agent_name="reviewer")
    await store.save(a)
    await store.save(b)
    listed = await store.list_sessions()
    ids = {s.session_id for s in listed}
    assert ids == {a.session_id, b.session_id}


async def test_list_sessions_finds_records_in_subdirectories(tmp_path: Path) -> None:
    """Regression: records written to <root>/<pool>/<id>.json must be visible.

    WorkspacePoolSessionStore partitions sessions by pool.  The flat
    LocalFileSessionStore used by the WebUI server must recursively discover
    those records so ``parent_session_id`` survives into the session list API.
    """
    store = LocalFileSessionStore(tmp_path)
    parent = SessionInfo(
        session_id="abc.main", agent_name="main", parent_session_id=None
    )
    child = SessionInfo(
        session_id="abc.reviewer",
        agent_name="reviewer",
        parent_session_id="abc.main",
    )
    (tmp_path / "main").mkdir()
    (tmp_path / "coding").mkdir()
    (tmp_path / "main" / "abc.main.json").write_text(parent.model_dump_json())
    (tmp_path / "coding" / "abc.reviewer.json").write_text(child.model_dump_json())

    listed = await store.list_sessions()
    by_sid = {s.session_id: s for s in listed}

    assert "abc.main" in by_sid
    assert "abc.reviewer" in by_sid
    assert by_sid["abc.main"].parent_session_id is None
    assert by_sid["abc.reviewer"].parent_session_id == "abc.main"


async def test_get_children_returns_only_children(
    tmp_path: Path, factory: SessionIdFactory
):
    store = LocalFileSessionStore(tmp_path)
    parent = factory.create(agent_name="main")
    child1 = factory.create(agent_name="reviewer", parent_session_id=parent)
    child2 = factory.create(agent_name="reviewer", parent_session_id=parent)
    other = factory.create(agent_name="main")
    for s in (parent, child1, child2, other):
        await store.save(s)
    children = await store.get_children(parent.session_id)
    child_ids = {c.session_id for c in children}
    assert child_ids == {child1.session_id, child2.session_id}
