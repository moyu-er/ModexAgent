"""Tests for WorkspacePoolSessionStore (pool-layered index)."""

from __future__ import annotations

import os as _os
from pathlib import Path

import pytest
from bot.service.session_store import WorkspacePoolSessionStore

from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_store import LocalFileSessionStore


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


def _pool_of(s: SessionInfo) -> str:
    return "coding" if s.agent_name != "main" else "main"


async def test_save_writes_under_pool_dir(
    tmp_path: Path, factory: SessionIdFactory
) -> None:
    """Save writes to ``<root>/<pool>/<safe_id>.json``."""
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    session = factory.create(agent_name="main", metadata={"pool": "coding"})
    await store.save(session)

    # File exists under the pool subdirectory.
    pool_dir = tmp_path / "main"
    assert pool_dir.is_dir()
    files = list(pool_dir.glob("*.json"))
    assert len(files) == 1
    assert session.session_id in files[0].name

    # Round-trip via get (scans all pool subdirs).
    got = await store.get(session.session_id)
    assert got is not None
    assert got == session
    assert got.metadata == {"pool": "coding"}
    assert got.created_at is not None


async def test_different_pools_land_in_different_dirs(
    tmp_path: Path, factory: SessionIdFactory
) -> None:
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


async def test_delete_removes_file(tmp_path: Path, factory: SessionIdFactory) -> None:
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    session = factory.create(agent_name="main")
    await store.save(session)
    assert await store.get(session.session_id) is not None
    await store.delete(session.session_id)
    assert await store.get(session.session_id) is None


async def test_list_and_children(
    tmp_path: Path, factory: SessionIdFactory
) -> None:
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    parent = factory.create(agent_name="main")
    child = factory.create(agent_name="reviewer", parent_session_id=parent)
    for s in (parent, child):
        await store.save(s)

    listed = await store.list_sessions()
    assert len(listed) == 2

    children = await store.get_children(parent.session_id)
    assert len(children) == 1
    assert children[0].session_id == child.session_id


# ── Atomic write (crash-safety) ────────────────────────────────────────────


async def test_pool_save_preserves_existing_record_if_replace_fails(
    tmp_path: Path, factory: SessionIdFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the final atomic replace fails, the existing record stays intact.

    Simulates a crash between writing the temp file and replacing the target.
    A non-atomic ``write_text`` would have overwritten the target in place,
    corrupting / losing the previous record.
    """
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    session = factory.create(agent_name="main")
    await store.save(session)
    original = await store.get(session.session_id)
    assert original is not None

    # Force the final replace step to fail (simulates mid-commit crash).
    monkeypatch.setattr(_os, "replace", _raise_runtime_error)

    updated = session.model_copy(update={"metadata": {"pool": "coding"}})
    with pytest.raises(RuntimeError):
        await store.save(updated)

    # Existing record is preserved unchanged — not partially overwritten.
    got = await store.get(session.session_id)
    assert got is not None
    assert got.metadata == original.metadata
    # No leftover temp artifacts in the index tree.
    assert not list(tmp_path.rglob("*.tmp.*"))


async def test_base_save_preserves_existing_record_if_replace_fails(
    tmp_path: Path, factory: SessionIdFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same atomicity guarantee for the base LocalFileSessionStore."""
    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main")
    await store.save(session)
    original = await store.get(session.session_id)
    assert original is not None

    monkeypatch.setattr(_os, "replace", _raise_runtime_error)

    updated = session.model_copy(update={"metadata": {"pool": "coding"}})
    with pytest.raises(RuntimeError):
        await store.save(updated)

    got = await store.get(session.session_id)
    assert got is not None
    assert got.metadata == original.metadata
    assert not list(tmp_path.rglob("*.tmp.*"))


def _raise_runtime_error(*args: object, **kwargs: object) -> None:
    raise RuntimeError("simulated crash during atomic replace")


# ── GC: conversation-scoped index cleanup ──────────────────────────────────


async def test_delete_sessions_by_prefix_removes_conversation_index(
    tmp_path: Path, factory: SessionIdFactory
) -> None:
    """Deleting a conversation's prefix removes all its index records.

    A conversation owns the main session plus every subagent invocation
    session (``abc.reviewer.<invocation>``), all sharing the conversation
    prefix. Deleting the conversation must clean up all of them, leaving other
    conversations untouched. Without this, subagent invocation index files
    accumulate as orphans forever.
    """
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    parent = factory.create(agent_name="main")
    # Subagents share the parent's conversation prefix (production creates them
    # via create_with_prefix, carrying the parent prefix verbatim).
    child = factory.create_with_prefix(
        prefix=parent.session_id_prefix,
        agent_name="reviewer",
        parent_session_id=parent,
    )
    other = factory.create(agent_name="main")  # different conversation
    for s in (parent, child, other):
        await store.save(s)
    assert parent.session_id_prefix == child.session_id_prefix
    assert parent.session_id_prefix != other.session_id_prefix

    await store.delete_sessions_by_prefix(parent.session_id_prefix)

    remaining = {s.session_id for s in await store.list_sessions()}
    assert parent.session_id not in remaining
    assert child.session_id not in remaining
    assert other.session_id in remaining  # other conversation untouched


async def test_delete_sessions_by_prefix_unknown_is_noop(
    tmp_path: Path, factory: SessionIdFactory
) -> None:
    store = WorkspacePoolSessionStore(tmp_path, pool_resolver=_pool_of)
    session = factory.create(agent_name="main")
    await store.save(session)

    await store.delete_sessions_by_prefix("does-not-exist")

    remaining = {s.session_id for s in await store.list_sessions()}
    assert session.session_id in remaining


