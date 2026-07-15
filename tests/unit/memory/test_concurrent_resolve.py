"""Regression tests for DefaultMemoryStoreRegistry concurrent resolve() race.

Issue: DefaultMemoryStoreRegistry.resolve() has a TOCTOU race — it checks
``_stores.get(cache_key)`` then creates a new scoped storage and
assigns ``_stores[cache_key] = storage``. Two concurrent calls resolving
the same key can both miss the cache, both create separate storage instances,
and the second assignment overwrites the first — causing data loss.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modex_agent.core.scope import (
    GlobalScope,
    MemoryContext,
    MemoryLayerName,
    SessionScope,
)
from modex_agent.memory.registry import DefaultMemoryStoreRegistry


def _make_context(session_id: str = "sess-1") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="user-1")


@pytest.mark.asyncio
async def test_concurrent_resolve_same_key_returns_identical_storage(tmp_path: Path) -> None:
    """Concurrent resolve() on the same (layer, scope_key) must return
    the exact same storage instance — never two different objects.

    If this fails, one caller's writes are silently lost.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    scope = SessionScope()
    ctx = _make_context()
    layer = MemoryLayerName.SESSION

    bundles = await asyncio.gather(
        *(registry.resolve(layer=layer, scope=scope, context=ctx) for _ in range(50))
    )

    first = bundles[0].messages
    for i, b in enumerate(bundles):
        assert b.messages is first, (
            f"resolve() returned a different store at index {i}. "
            f"Race condition: {50 - i} callers got a different storage instance."
        )

    await registry.close()


@pytest.mark.asyncio
async def test_concurrent_resolve_with_yield_returns_identical_storage(tmp_path: Path) -> None:
    """Force asyncio context switches inside resolve() by monkey-patching
    DefaultScopedStorage.initialize() to yield (simulating file I/O latency).

    This exposes the TOCTOU race: two coroutines both see cache_key missing,
    both create separate storage instances, second overwrites first.
    """
    import unittest.mock

    from modex_agent.memory.stores.scoped_file import DefaultScopedStorage

    registry = DefaultMemoryStoreRegistry(tmp_path)
    scope = SessionScope()
    ctx = _make_context()
    layer = MemoryLayerName.SESSION

    async def _slow_initialize(self: DefaultScopedStorage) -> None:
        await asyncio.sleep(0)

    with unittest.mock.patch.object(
        DefaultScopedStorage, "initialize", _slow_initialize
    ):
        bundles = await asyncio.gather(
            *(
                registry.resolve(layer=layer, scope=scope, context=ctx)
                for _ in range(50)
            )
        )

    first = bundles[0].messages
    unique_ids = set(id(b.messages) for b in bundles)
    assert len(unique_ids) == 1, (
        f"Race detected: got {len(unique_ids)} different storage instances "
        f"from 50 concurrent resolve() calls. "
        f"DefaultMemoryStoreRegistry.resolve() has a TOCTOU race."
    )

    await registry.close()


@pytest.mark.asyncio
async def test_concurrent_resolve_no_data_loss(tmp_path: Path) -> None:
    """Two concurrent resolve + write sequences must not lose data.

    Pattern: two tasks resolve the same key, each writes a value,
    then we verify BOTH values are readable from the storage.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    scope = GlobalScope()
    ctx = _make_context()
    layer = MemoryLayerName.SESSION

    async def write_key(key: str, value: str) -> None:
        bundle = await registry.resolve(layer=layer, scope=scope, context=ctx)
        await bundle.kv.set(key, value)

    await asyncio.gather(
        write_key("key-a", "value-a"),
        write_key("key-b", "value-b"),
    )

    bundle = await registry.resolve(layer=layer, scope=scope, context=ctx)
    val_a = await bundle.kv.get("key-a")
    val_b = await bundle.kv.get("key-b")

    assert val_a == "value-a", f"key-a lost! Got {val_a!r} — one of the storages was dropped"
    assert val_b == "value-b", f"key-b lost! Got {val_b!r} — one of the storages was dropped"

    await registry.close()


@pytest.mark.asyncio
async def test_concurrent_resolve_different_keys_are_isolated(tmp_path: Path) -> None:
    """Different scope keys must produce different storages.

    This should pass even with the race, but verifies isolation.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    scope = SessionScope()  # key = session_id
    layer = MemoryLayerName.SESSION

    ctx_a = _make_context(session_id="sess-a")
    ctx_b = _make_context(session_id="sess-b")

    bundle_a, bundle_b = await asyncio.gather(
        registry.resolve(layer=layer, scope=scope, context=ctx_a),
        registry.resolve(layer=layer, scope=scope, context=ctx_b),
    )

    assert bundle_a.messages is not bundle_b.messages, "Different sessions must use different storages"

    await bundle_a.kv.set("secret", "a-only")
    val_b = await bundle_b.kv.get("secret")
    assert val_b is None, f"Session B saw session A's data: {val_b!r}"

    await registry.close()
