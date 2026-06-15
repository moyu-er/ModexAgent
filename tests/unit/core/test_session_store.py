"""Tests for LocalFileSessionStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from framework.core.session_id import SessionIdFactory
from framework.core.session_store import LocalFileSessionStore


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_save_and_get_roundtrip(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main", metadata={"pool": "coding"})
    await store.save(session)
    got = await store.get(str(session))
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
    await store.delete(str(session))
    assert await store.get(str(session)) is None


async def test_list_sessions_returns_all(tmp_path: Path, factory: SessionIdFactory):
    store = LocalFileSessionStore(tmp_path)
    a = factory.create(agent_name="main")
    b = factory.create(agent_name="reviewer")
    await store.save(a)
    await store.save(b)
    listed = await store.list_sessions()
    ids = {str(s) for s in listed}
    assert ids == {str(a), str(b)}


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
    children = await store.get_children(str(parent))
    child_ids = {str(c) for c in children}
    assert child_ids == {str(child1), str(child2)}
