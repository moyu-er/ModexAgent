"""Regression tests for InMemoryStoreRegistry concurrent resolve() race.

Issue: InMemoryStoreRegistry.resolve() has a TOCTOU race — it checks
``_stores.get(cache_key)`` then creates a new InMemoryScopedStorage and
assigns ``_stores[cache_key] = storage``. Two concurrent calls resolving
the same key can both miss the cache, both create separate storage instances,
and the second assignment overwrites the first — causing data loss.
"""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.memory.core.scope import (
    GlobalScope,
    MemoryContext,
    MemoryLayerName,
    SessionScope,
)
from modex_agent.memory.registry.in_memory import InMemoryStoreRegistry


def _make_context(session_id: str = "sess-1") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="user-1")


@pytest.mark.asyncio
async def test_concurrent_resolve_same_key_returns_identical_storage() -> None:
    """Concurrent resolve() on the same (layer, scope_key) must return
    the exact same storage instance — never two different objects.

    If this fails, one caller's writes are silently lost.
    """
    registry = InMemoryStoreRegistry()
    scope = SessionScope()
    ctx = _make_context()
    layer = MemoryLayerName.SESSION

    # Launch 50 concurrent resolves for the same key
    storages = await asyncio.gather(
        *(registry.resolve(layer=layer, scope=scope, context=ctx) for _ in range(50))
    )

    # All must be the SAME object (identity check, not equality)
    first = storages[0]
    for i, s in enumerate(storages):
        assert s is first, (
            f"resolve() returned a different object at index {i}. "
            f"Race condition: {50 - i} callers got a different storage instance."
        )

    await registry.close()


@pytest.mark.asyncio
async def test_concurrent_resolve_with_yield_returns_identical_storage() -> None:
    """Force asyncio context switches inside resolve() by monkey-patching
    InMemoryScopedStorage.initialize() to yield (simulating file-based storage).

    This exposes the TOCTOU race: two coroutines both see cache_key missing,
    both create separate storage instances, second overwrites first.
    """
    import unittest.mock

    from modex_agent.memory.stores.scoped_in_memory import InMemoryScopedStorage

    registry = InMemoryStoreRegistry()
    scope = SessionScope()
    ctx = _make_context()
    layer = MemoryLayerName.SESSION

    # Force initialize() to yield — simulates real I/O latency
    async def _slow_initialize(self: InMemoryScopedStorage) -> None:
        await asyncio.sleep(0)

    with unittest.mock.patch.object(
        InMemoryScopedStorage, "initialize", _slow_initialize
    ):
        storages = await asyncio.gather(
            *(
                registry.resolve(layer=layer, scope=scope, context=ctx)
                for _ in range(50)
            )
        )

    # All must be the SAME object
    first = storages[0]
    unique_ids = set(id(s) for s in storages)
    assert len(unique_ids) == 1, (
        f"Race detected: got {len(unique_ids)} different storage instances "
        f"from 50 concurrent resolve() calls. "
        f"InMemoryStoreRegistry.resolve() has a TOCTOU race."
    )

    await registry.close()


@pytest.mark.asyncio
async def test_concurrent_resolve_no_data_loss() -> None:
    """Two concurrent resolve + write sequences must not lose data.

    Pattern: two tasks resolve the same key, each writes a value,
    then we verify BOTH values are readable from the storage.
    """
    registry = InMemoryStoreRegistry()
    scope = GlobalScope()
    ctx = _make_context()
    layer = MemoryLayerName.SESSION

    async def write_key(key: str, value: str) -> None:
        storage = await registry.resolve(layer=layer, scope=scope, context=ctx)
        await storage.set(key, value)

    # Concurrent writes to the SAME storage via separate resolve() calls
    await asyncio.gather(
        write_key("key-a", "value-a"),
        write_key("key-b", "value-b"),
    )

    # Verify the SINGLE storage has both keys
    storage = await registry.resolve(layer=layer, scope=scope, context=ctx)
    val_a = await storage.get("key-a")
    val_b = await storage.get("key-b")

    assert val_a == "value-a", f"key-a lost! Got {val_a!r} — one of the storages was dropped"
    assert val_b == "value-b", f"key-b lost! Got {val_b!r} — one of the storages was dropped"

    await registry.close()


@pytest.mark.asyncio
async def test_concurrent_resolve_different_keys_are_isolated() -> None:
    """Different scope keys must produce different storages.

    This should pass even with the race, but verifies isolation.
    """
    registry = InMemoryStoreRegistry()
    scope = SessionScope()  # key = session_id
    layer = MemoryLayerName.SESSION

    ctx_a = _make_context(session_id="sess-a")
    ctx_b = _make_context(session_id="sess-b")

    storage_a, storage_b = await asyncio.gather(
        registry.resolve(layer=layer, scope=scope, context=ctx_a),
        registry.resolve(layer=layer, scope=scope, context=ctx_b),
    )

    # Must be different objects
    assert storage_a is not storage_b, "Different sessions must use different storages"

    # Write to A, verify B doesn't see it
    await storage_a.set("secret", "a-only")
    val_b = await storage_b.get("secret")
    assert val_b is None, f"Session B saw session A's data: {val_b!r}"

    await registry.close()
