"""Tests for SubagentPool — LRU instance reuse for dynamic subagents."""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.multi_agent.pool_reuse import SubagentPool


class _FakeInstance:
    """Minimal fake agent instance for pool tests."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.pipeline = None


# -- helpers ----------------------------------------------------------------

async def _make_factory(name: str, call_count: list[int]):
    """Return a factory that increments *call_count* and returns a fake instance."""

    async def factory() -> _FakeInstance:
        call_count[0] += 1
        return _FakeInstance(name)

    return factory


# -- tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acquire_creates_on_miss() -> None:
    """Factory is called on cache miss; instance is returned."""
    pool = SubagentPool(max_size=4)
    call_count = [0]
    factory = await _make_factory("researcher", call_count)

    instance = await pool.acquire("researcher", factory)

    assert isinstance(instance, _FakeInstance)
    assert instance.name == "researcher"
    assert call_count[0] == 1
    assert pool.size == 1
    assert "researcher" in pool.cached_types


@pytest.mark.asyncio
async def test_acquire_returns_cached_on_hit() -> None:
    """Factory is called only once for the same agent_type."""
    pool = SubagentPool(max_size=4)
    call_count = [0]
    factory = await _make_factory("researcher", call_count)

    first = await pool.acquire("researcher", factory)
    second = await pool.acquire("researcher", factory)

    assert first is second
    assert call_count[0] == 1  # factory called only once


@pytest.mark.asyncio
async def test_lru_eviction_on_full_pool() -> None:
    """Oldest entry is evicted when pool is full."""
    pool = SubagentPool(max_size=2)

    # Fill the pool
    await pool.acquire("type_a", await _make_factory("a", [0]))
    await pool.acquire("type_b", await _make_factory("b", [0]))

    assert pool.size == 2

    # Adding a third should evict type_a (oldest)
    await pool.acquire("type_c", await _make_factory("c", [0]))

    assert pool.size == 2
    assert "type_a" not in pool.cached_types
    assert "type_b" in pool.cached_types
    assert "type_c" in pool.cached_types


@pytest.mark.asyncio
async def test_evict_removes_entry() -> None:
    """Explicit evict() removes the entry from the pool."""
    pool = SubagentPool(max_size=4)
    await pool.acquire("target", await _make_factory("target", [0]))
    assert "target" in pool.cached_types

    await pool.evict("target")

    assert "target" not in pool.cached_types
    assert pool.size == 0


@pytest.mark.asyncio
async def test_close_evicts_all() -> None:
    """close() clears the entire pool."""
    pool = SubagentPool(max_size=8)
    await pool.acquire("a", await _make_factory("a", [0]))
    await pool.acquire("b", await _make_factory("b", [0]))
    await pool.acquire("c", await _make_factory("c", [0]))
    assert pool.size == 3

    await pool.close()

    assert pool.size == 0
    assert pool.cached_types == []


@pytest.mark.asyncio
async def test_multiple_types_isolated() -> None:
    """Different agent types get different instances."""
    pool = SubagentPool(max_size=8)

    inst_a = await pool.acquire("type_a", await _make_factory("a", [0]))
    inst_b = await pool.acquire("type_b", await _make_factory("b", [0]))

    assert inst_a is not inst_b
    assert inst_a.name == "a"
    assert inst_b.name == "b"
    assert pool.size == 2

    # Re-acquiring either type returns the same cached instance
    inst_a2 = await pool.acquire("type_a", await _make_factory("a_dup", [0]))
    inst_b2 = await pool.acquire("type_b", await _make_factory("b_dup", [0]))

    assert inst_a2 is inst_a
    assert inst_b2 is inst_b
    assert inst_a2.name == "a"  # name unchanged — factory not called again
    assert inst_b2.name == "b"
