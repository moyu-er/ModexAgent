"""Tests for WorkspacePoolSessionStore (pool-layered index)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.service.session_store import WorkspacePoolSessionStore
from framework.core.session_id import SessionId, SessionIdFactory


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


def _pool_of(s: SessionId) -> str:
    return "coding" if s.agent_name != "main" else "main"


async def test_save_writes_under_pool_dir(
    tmp_path: Path, factory: SessionIdFactory
):
    """Save writes to ``<root>/<pool>/<safe_id>.json``."""
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    session = factory.create(agent_name="main", metadata={"pool": "coding"})
    await store.save(session)

    # File exists under the pool subdirectory.
    pool_dir = tmp_path / "main"
    assert pool_dir.is_dir()
    files = list(pool_dir.glob("*.json"))
    assert len(files) == 1
    assert str(session) in files[0].name

    # Round-trip via get (scans all pool subdirs).
    got = await store.get(str(session))
    assert got is not None
    assert got == session
    assert got.metadata == {"pool": "coding"}
    assert got.created_at is not None


async def test_different_pools_land_in_different_dirs(
    tmp_path: Path, factory: SessionIdFactory
):
    """Main and coding pool sessions go to separate directories."""
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    main_s = factory.create(agent_name="main")
    coding_s = factory.create(agent_name="coding")
    await store.save(main_s)
    await store.save(coding_s)

    assert (tmp_path / "main").is_dir()
    assert (tmp_path / "coding").is_dir()
    listed = await store.list_sessions()
    assert len(listed) == 2


async def test_delete_removes_file(tmp_path: Path, factory: SessionIdFactory):
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    session = factory.create(agent_name="main")
    await store.save(session)
    assert await store.get(str(session)) is not None
    await store.delete(str(session))
    assert await store.get(str(session)) is None


async def test_list_and_children(
    tmp_path: Path, factory: SessionIdFactory
):
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    parent = factory.create(agent_name="main")
    child = factory.create(agent_name="reviewer", parent_session_id=parent)
    for s in (parent, child):
        await store.save(s)

    listed = await store.list_sessions()
    assert len(listed) == 2

    children = await store.get_children(str(parent))
    assert len(children) == 1
    assert str(children[0]) == str(child)
