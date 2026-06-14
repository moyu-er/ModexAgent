"""Tests for InMemorySessionRegistry."""

from __future__ import annotations

import asyncio

import pytest

from framework.core.session_id import SessionIdFactory
from framework.core.session_registry import InMemorySessionRegistry


@pytest.fixture
def factory() -> SessionIdFactory:
    return SessionIdFactory()


async def test_register_and_get(factory: SessionIdFactory):
    reg = InMemorySessionRegistry()
    session = factory.create(agent_name="main")
    await reg.register(session)
    assert await reg.get(str(session)) == session


async def test_get_missing_returns_none():
    reg = InMemorySessionRegistry()
    assert await reg.get("nope.main") is None


async def test_touch_updates_updated_at(factory: SessionIdFactory):
    reg = InMemorySessionRegistry()
    session = factory.create(agent_name="main")
    await reg.register(session)
    before = (await reg.get(str(session))).updated_at
    await asyncio.sleep(0.01)
    await reg.touch(str(session))
    after = (await reg.get(str(session))).updated_at
    assert after > before


async def test_register_writes_through_to_store(tmp_path, factory: SessionIdFactory):
    from framework.core.session_store import LocalFileSessionStore

    store = LocalFileSessionStore(tmp_path)
    reg = InMemorySessionRegistry(store=store)
    session = factory.create(agent_name="main")
    await reg.register(session)
    # store now has the record
    assert await store.get(str(session)) == session


async def test_load_all_populates_cache_from_store(tmp_path, factory: SessionIdFactory):
    from framework.core.session_store import LocalFileSessionStore

    store = LocalFileSessionStore(tmp_path)
    session = factory.create(agent_name="main")
    await store.save(session)
    reg = InMemorySessionRegistry(store=store)
    await reg.load_all()
    assert await reg.get(str(session)) == session


async def test_concurrent_register_is_safe(factory: SessionIdFactory):
    """Two coroutines registering different sessions must not lose data."""
    reg = InMemorySessionRegistry()
    sessions = [factory.create(agent_name=f"agent{i}") for i in range(20)]
    await asyncio.gather(*(reg.register(s) for s in sessions))
    for s in sessions:
        assert await reg.get(str(s)) is not None
