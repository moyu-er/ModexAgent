"""PoolRoutingStore conformance — same assertions for ``file`` and ``sqlite``.

Both backends are synchronous (the ABC is sync). The SQLite adapter opens its
own ``sqlite3`` connection; the DB file must already exist and be migrated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from modex_agent.multi_agent.pool_router import (
    LocalFilePoolRoutingStore,
    PoolRoutingStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.pool_routing_store import SqlitePoolRoutingStore


@pytest.fixture(params=["file", "sqlite"])
def pool_routing_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> Iterator[PoolRoutingStore]:
    """Parametrized PoolRoutingStore — file or sqlite (both sync)."""
    if request.param == "file":
        yield LocalFilePoolRoutingStore(tmp_path / "pool_sessions")
        return
    # SQLite: open + close ConnectionManager to create+migrate the DB,
    # then SqlitePoolRoutingStore opens its own sync sqlite3 connection.
    db_path = tmp_path / "workspace.db"
    mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    asyncio.run(mgr.open())
    asyncio.run(mgr.close())
    store = SqlitePoolRoutingStore(db_path)
    yield store
    store.close()


class TestPoolRoutingStoreConformance:
    """Same behavior on both backends."""

    def test_get_missing_returns_none(self, pool_routing_store: PoolRoutingStore) -> None:
        assert pool_routing_store.get_pool("s1") is None

    def test_set_then_get(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.set_pool("s1", "default")
        assert pool_routing_store.get_pool("s1") == "default"

    def test_set_overwrites(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.set_pool("s1", "pool_a")
        pool_routing_store.set_pool("s1", "pool_b")
        assert pool_routing_store.get_pool("s1") == "pool_b"

    def test_delete_removes_route(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.set_pool("s1", "default")
        pool_routing_store.delete_pool("s1")
        assert pool_routing_store.get_pool("s1") is None

    def test_delete_missing_is_noop(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.delete_pool("nope")  # must not raise

    def test_list_prefixes_empty(self, pool_routing_store: PoolRoutingStore) -> None:
        assert pool_routing_store.list_prefixes() == []

    def test_list_prefixes_sorted(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.set_pool("s2", "default")
        pool_routing_store.set_pool("s1", "default")
        assert pool_routing_store.list_prefixes() == ["s1", "s2"]

    # -- delete_pool_routes --

    def test_delete_pool_routes_removes_only_matching(
        self, pool_routing_store: PoolRoutingStore
    ) -> None:
        pool_routing_store.set_pool("s1", "pool_a")
        pool_routing_store.set_pool("s2", "pool_b")
        pool_routing_store.set_pool("s3", "pool_a")
        pool_routing_store.set_pool("s4", "pool_c")

        deleted = pool_routing_store.delete_pool_routes("pool_a")

        assert deleted == 2
        assert pool_routing_store.get_pool("s1") is None
        assert pool_routing_store.get_pool("s3") is None
        assert pool_routing_store.get_pool("s2") == "pool_b"
        assert pool_routing_store.get_pool("s4") == "pool_c"
        assert sorted(pool_routing_store.list_prefixes()) == ["s2", "s4"]

    def test_delete_pool_routes_no_match_returns_zero(
        self, pool_routing_store: PoolRoutingStore
    ) -> None:
        pool_routing_store.set_pool("s1", "pool_a")
        deleted = pool_routing_store.delete_pool_routes("nonexistent")
        assert deleted == 0
        assert pool_routing_store.get_pool("s1") == "pool_a"

    def test_delete_pool_routes_empty_store_returns_zero(
        self, pool_routing_store: PoolRoutingStore
    ) -> None:
        assert pool_routing_store.delete_pool_routes("any") == 0

    # -- convenience .get() / .set() aliases (used by ResolvePoolStage etc.) --

    def test_alias_get_missing_returns_none(self, pool_routing_store: PoolRoutingStore) -> None:
        assert pool_routing_store.get("s1") is None

    def test_alias_get_missing_returns_default(self, pool_routing_store: PoolRoutingStore) -> None:
        assert pool_routing_store.get("s1", "fallback") == "fallback"

    def test_alias_set_then_get(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.set("s1", "default")
        assert pool_routing_store.get("s1") == "default"
        assert pool_routing_store.get("s1", "fallback") == "default"

    def test_alias_set_overwrites(self, pool_routing_store: PoolRoutingStore) -> None:
        pool_routing_store.set("s1", "pool_a")
        pool_routing_store.set("s1", "pool_b")
        assert pool_routing_store.get("s1") == "pool_b"
