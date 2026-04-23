"""Tests for ShortTermMessageHistory and ListMessageHistory."""

import pytest

from framework.memory.core.scope import MemoryContext, SessionScope
from framework.memory.history import ListMessageHistory, ShortTermMessageHistory
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.managers.working import WorkingMemoryManager
from framework.memory.stores.in_memory import InMemoryStorage


@pytest.mark.asyncio
async def test_list_message_history_basic():
    hist = ListMessageHistory()
    await hist.append({"role": "user", "content": "hi"})
    await hist.extend([{"role": "assistant", "content": "hello"}])
    msgs = await hist.to_list()
    assert len(msgs) == 2
    assert msgs[0]["content"] == "hi"
    assert msgs[1]["content"] == "hello"


@pytest.mark.asyncio
async def test_short_term_message_history_append_and_read():
    storage = InMemoryStorage()
    scope = SessionScope()
    manager = ShortTermMemoryManager(storage, scope, config=ShortTermConfig())
    ctx = MemoryContext(session_id="s1", user_id="u1")

    hist = ShortTermMessageHistory(manager, ctx)
    await hist.append({"role": "user", "content": "a"})
    await hist.append({"role": "assistant", "content": "b"})

    msgs = await hist.to_list()
    assert len(msgs) == 2
    assert msgs[0]["content"] == "a"
    assert msgs[1]["content"] == "b"


@pytest.mark.asyncio
async def test_short_term_message_history_extend():
    storage = InMemoryStorage()
    scope = SessionScope()
    manager = ShortTermMemoryManager(storage, scope, config=ShortTermConfig())
    ctx = MemoryContext(session_id="s2", user_id="u1")

    hist = ShortTermMessageHistory(manager, ctx)
    await hist.extend([
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ])

    msgs = await hist.to_list()
    assert len(msgs) == 2
    assert msgs[1]["content"] == "y"


@pytest.mark.asyncio
async def test_short_term_message_history_merges_working_memory():
    storage = InMemoryStorage()
    scope = SessionScope()
    manager = ShortTermMemoryManager(storage, scope, config=ShortTermConfig())
    working = WorkingMemoryManager(scope)
    ctx = MemoryContext(session_id="s3", user_id="u1")

    hist = ShortTermMessageHistory(manager, ctx, working_manager=working)
    await hist.append({"role": "user", "content": "stm"})
    working.add_message(ctx, {"role": "tool", "content": "working"})

    msgs = await hist.to_list()
    assert len(msgs) == 2
    assert msgs[0]["content"] == "stm"
    assert msgs[1]["content"] == "working"


@pytest.mark.asyncio
async def test_short_term_message_history_cache_invalidation():
    storage = InMemoryStorage()
    scope = SessionScope()
    manager = ShortTermMemoryManager(storage, scope, config=ShortTermConfig())
    ctx = MemoryContext(session_id="s4", user_id="u1")

    hist = ShortTermMessageHistory(manager, ctx)
    await hist.append({"role": "user", "content": "first"})
    _ = await hist.to_list()
    assert hist._cache is not None

    await hist.append({"role": "user", "content": "second"})
    assert hist._cache is None

    msgs = await hist.to_list()
    assert len(msgs) == 2


@pytest.mark.asyncio
async def test_short_term_message_history_to_list_populates_cache():
    storage = InMemoryStorage()
    scope = SessionScope()
    manager = ShortTermMemoryManager(storage, scope, config=ShortTermConfig())
    ctx = MemoryContext(session_id="s5", user_id="u1")

    hist = ShortTermMessageHistory(manager, ctx)
    await hist.extend([{"role": "user", "content": "1"}])
    assert hist._cache is None

    await hist.to_list()
    assert hist._cache is not None
    assert len(hist._cache) == 1

