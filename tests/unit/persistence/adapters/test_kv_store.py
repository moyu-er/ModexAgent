"""Tests for :class:`SqliteKVStore` — CRUD + scope isolation."""

from __future__ import annotations

from typing import Any

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager
from modex_agent.persistence.adapters.kv_store import SqliteKVStore


class TestKVCRUD:
    async def test_get_missing_key_returns_none(self, kv_store: SqliteKVStore) -> None:
        assert await kv_store.get("missing") is None

    async def test_set_and_get_roundtrip(self, kv_store: SqliteKVStore) -> None:
        await kv_store.set("k1", {"value": 42})

        result = await kv_store.get("k1")
        assert result == {"value": 42}

    async def test_set_overwrites(self, kv_store: SqliteKVStore) -> None:
        await kv_store.set("k1", "old")
        await kv_store.set("k1", "new")

        assert await kv_store.get("k1") == "new"

    async def test_set_stores_complex_value(self, kv_store: SqliteKVStore) -> None:
        value: dict[str, Any] = {"nested": {"list": [1, 2, 3]}, "flag": True}
        await kv_store.set("complex", value)

        assert await kv_store.get("complex") == value

    async def test_delete_existing_returns_true(self, kv_store: SqliteKVStore) -> None:
        await kv_store.set("k1", "v1")

        assert await kv_store.delete("k1") is True
        assert await kv_store.get("k1") is None

    async def test_delete_missing_returns_false(self, kv_store: SqliteKVStore) -> None:
        assert await kv_store.delete("missing") is False


class TestKVListKeys:
    async def test_list_keys_empty(self, kv_store: SqliteKVStore) -> None:
        assert await kv_store.list_keys() == []

    async def test_list_keys_all(self, kv_store: SqliteKVStore) -> None:
        await kv_store.set("alpha", 1)
        await kv_store.set("beta", 2)
        await kv_store.set("gamma", 3)

        assert await kv_store.list_keys() == ["alpha", "beta", "gamma"]

    async def test_list_keys_with_prefix(self, kv_store: SqliteKVStore) -> None:
        await kv_store.set("user:name", "a")
        await kv_store.set("user:age", 30)
        await kv_store.set("config:mode", "dev")

        assert await kv_store.list_keys("user:") == ["user:age", "user:name"]

    async def test_list_keys_empty_prefix_returns_all(self, kv_store: SqliteKVStore) -> None:
        await kv_store.set("k1", 1)
        await kv_store.set("k2", 2)

        assert await kv_store.list_keys("") == ["k1", "k2"]


class TestKVScopeIsolation:
    async def test_separate_scopes_are_isolated(
        self, connection: ConnectionManager, scope: RecordScope, other_scope: RecordScope
    ) -> None:
        store_a = SqliteKVStore(connection, scope)
        store_b = SqliteKVStore(connection, other_scope)

        await store_a.set("k1", "a")
        await store_b.set("k1", "b")

        assert await store_a.get("k1") == "a"
        assert await store_b.get("k1") == "b"
