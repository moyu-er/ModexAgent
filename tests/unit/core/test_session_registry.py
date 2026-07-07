"""Tests for InMemorySessionRegistry."""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_register_and_get(factory: SessionIdFactory):
    reg = InMemorySessionRegistry()
    session = factory.create(agent_name="main")
    await reg.register(session)
    assert await reg.get(session.session_id) == session


async def test_register_initializes_timestamps_for_from_str() -> None:
    """Reconstructing a SessionInfo from a bare id (as AgentPool._track_session
    does) must not leave created_at/updated_at null in the persisted record."""
    from modex_agent.core.session_id import SessionInfo

    reg = InMemorySessionRegistry()
    session = SessionInfo.from_str("abc123.main", default_agent_name="main")
    assert session.created_at is None
    assert session.updated_at is None

    await reg.register(session)

    stored = await reg.get("abc123.main")
    assert stored is not None
    assert isinstance(stored.created_at, int)
    assert isinstance(stored.updated_at, int)


async def test_get_missing_returns_none():
    reg = InMemorySessionRegistry()
    assert await reg.get("nope.main") is None


async def test_touch_updates_updated_at(factory: SessionIdFactory):
    reg = InMemorySessionRegistry()
    session = factory.create(agent_name="main")
    await reg.register(session)
    before = (await reg.get(session.session_id)).updated_at
    await asyncio.sleep(0.01)
    await reg.touch(session.session_id)
    after = (await reg.get(session.session_id)).updated_at
    assert after > before


async def test_register_writes_through_to_store(tmp_path, factory: SessionIdFactory):
    from modex_agent.core.session_store import LocalFileSessionStore

    store = LocalFileSessionStore(tmp_path)
    reg = InMemorySessionRegistry(store=store)
    session = factory.create(agent_name="main")
    await reg.register(session)
    # store now has the record
    assert await store.get(session.session_id) == session


async def test_load_all_populates_cache_from_store(tmp_path, factory: SessionIdFactory):
    from modex_agent.core.session_store import LocalFileSessionStore

    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main")
    await store.save(session)
    reg = InMemorySessionRegistry(store=store)
    await reg.load_all()
    assert await reg.get(session.session_id) == session


async def test_register_does_not_reparent_existing_session():
    """A later register() for the same session must NOT overwrite an already
    established parent_session_id.

    Regression for the cascade in the phantom-session bug: a phantom main
    session reused a subagent's invocation_id to resume it, and register()
    blindly overwrote the subagent's parent_session_id, orphaning it from the
    real parent. The established parent is authoritative; only fill it in when
    missing.
    """
    from modex_agent.core.session_id import SessionInfo

    reg = InMemorySessionRegistry()
    worker = SessionInfo(
        session_id="task-42.worker",
        agent_name="worker",
        parent_session_id="real.coding",
    )
    await reg.register(worker)

    # Same session id, DIFFERENT parent (the phantom-session reuse path).
    hijack = SessionInfo(
        session_id="task-42.worker",
        agent_name="worker",
        parent_session_id="phantom.coding",
    )
    await reg.register(hijack)

    record = await reg.get("task-42.worker")
    assert record is not None
    assert record.parent_session_id == "real.coding"


async def test_register_fills_parent_when_missing():
    """When no parent is recorded yet, register() must still populate it."""
    from modex_agent.core.session_id import SessionInfo

    reg = InMemorySessionRegistry()
    orphan = SessionInfo(
        session_id="task-99.worker",
        agent_name="worker",
        parent_session_id=None,
    )
    await reg.register(orphan)

    parented = SessionInfo(
        session_id="task-99.worker",
        agent_name="worker",
        parent_session_id="late.coding",
    )
    await reg.register(parented)

    record = await reg.get("task-99.worker")
    assert record is not None
    assert record.parent_session_id == "late.coding"


async def test_concurrent_register_is_safe(factory: SessionIdFactory):
    """Two coroutines registering different sessions must not lose data."""
    reg = InMemorySessionRegistry()
    sessions = [factory.create(agent_name=f"agent{i}") for i in range(20)]
    await asyncio.gather(*(reg.register(s) for s in sessions))
    for s in sessions:
        assert await reg.get(s.session_id) is not None
