"""KVStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`DefaultScopedStorage` (one instance implementing all four split
store ABCs).
SQLite: :class:`SqliteKVStore` (independent adapter over ``ConnectionManager``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.memory.core.split_stores import KVStore
from modex_agent.memory.scope import MemoryLayerName
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.kv_store import SqliteKVStore


@pytest.fixture(params=["file", "sqlite"])
async def kv_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[KVStore]:
    """Parametrized KVStore — file (DefaultScopedStorage) or sqlite."""
    if request.param == "file":
        store = DefaultScopedStorage(
            tmp_path / "kv_file",
            layer=MemoryLayerName.SESSION,
        )
        yield store
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteKVStore(mgr, scope)
        await mgr.close()


class TestKVStoreConformance:
    """Same behavior on both backends."""

    async def test_get_missing_returns_none(self, kv_store: KVStore) -> None:
        assert await kv_store.get("nope") is None

    async def test_set_then_get_roundtrip(self, kv_store: KVStore) -> None:
        await kv_store.set("k1", {"v": 42})
        assert await kv_store.get("k1") == {"v": 42}

    async def test_set_overwrites(self, kv_store: KVStore) -> None:
        await kv_store.set("k1", "old")
        await kv_store.set("k1", "new")
        assert await kv_store.get("k1") == "new"

    async def test_delete_existing_returns_true(self, kv_store: KVStore) -> None:
        await kv_store.set("k1", 1)
        assert await kv_store.delete("k1") is True
        assert await kv_store.get("k1") is None

    async def test_delete_missing_returns_false(self, kv_store: KVStore) -> None:
        assert await kv_store.delete("nope") is False

    async def test_list_keys_empty(self, kv_store: KVStore) -> None:
        assert await kv_store.list_keys() == []

    async def test_list_keys_all(self, kv_store: KVStore) -> None:
        await kv_store.set("a", 1)
        await kv_store.set("b", 2)
        assert await kv_store.list_keys() == ["a", "b"]

    async def test_list_keys_with_prefix(self, kv_store: KVStore) -> None:
        await kv_store.set("user:a", 1)
        await kv_store.set("user:b", 2)
        await kv_store.set("sys:c", 3)
        assert await kv_store.list_keys("user:") == ["user:a", "user:b"]

    async def test_list_keys_empty_prefix_returns_all(self, kv_store: KVStore) -> None:
        await kv_store.set("x", 1)
        await kv_store.set("y", 2)
        assert await kv_store.list_keys("") == ["x", "y"]
