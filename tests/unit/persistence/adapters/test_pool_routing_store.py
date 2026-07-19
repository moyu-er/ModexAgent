"""Tests for :class:`SqlitePoolRoutingStore`.

Covers pool routing CRUD. Corruption detection was removed in ADR-0028 (the
``pool`` generated column and the ``scope`` column were dropped); the
``PoolRoutingCorruptionError`` class is retained only for backward
compatibility and is no longer raised by the adapter.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters import (
    SqlitePoolRoutingStore,
)


async def _open_routing(
    tmp_path: Path,
) -> tuple[ConnectionManager, SqlitePoolRoutingStore]:
    """Open the workspace DB (runs migrations) then create the sync store."""
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    store = SqlitePoolRoutingStore(tmp_path / "state.db")
    return manager, store


# ── CRUD ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pool_nonexistent_returns_none(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        assert store.get_pool("no-such-prefix") is None
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_set_pool_and_get_roundtrips(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-1", "coding")
        assert store.get_pool("sess-1") == "coding"
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_set_pool_upsert_overwrites(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-2", "coding")
        store.set_pool("sess-2", "engineering")
        assert store.get_pool("sess-2") == "engineering"
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_delete_pool_removes_route(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-3", "coding")
        store.delete_pool("sess-3")
        assert store.get_pool("sess-3") is None
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_delete_pool_nonexistent_is_noop(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.delete_pool("never-existed")  # must not raise
    finally:
        store.close()
        await manager.close()


# ── list_prefixes ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_prefixes_returns_sorted(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-b", "coding")
        store.set_pool("sess-a", "main")
        store.set_pool("sess-c", "coding")
        assert store.list_prefixes() == ["sess-a", "sess-b", "sess-c"]
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_list_prefixes_empty_when_no_routes(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        assert store.list_prefixes() == []
    finally:
        store.close()
        await manager.close()


# ── delete_pool_routes ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_pool_routes_removes_only_matching(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-a", "pool_a")
        store.set_pool("sess-b", "pool_b")
        store.set_pool("sess-c", "pool_a")

        deleted = store.delete_pool_routes("pool_a")

        assert deleted == 2
        assert store.get_pool("sess-a") is None
        assert store.get_pool("sess-c") is None
        assert store.get_pool("sess-b") == "pool_b"
        assert store.list_prefixes() == ["sess-b"]
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_delete_pool_routes_no_match_returns_zero(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-1", "pool_a")
        deleted = store.delete_pool_routes("nonexistent")
        assert deleted == 0
        assert store.get_pool("sess-1") == "pool_a"
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_delete_pool_routes_empty_table_returns_zero(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        assert store.delete_pool_routes("any") == 0
    finally:
        store.close()
        await manager.close()
