"""Tests for :class:`SqlitePoolRoutingStore`.

Covers pool routing CRUD, ``rename_pool`` atomicity (single UPDATE), and
corruption detection (explicit error instead of silent default fallback).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters import (
    PoolRoutingCorruptionError,
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


# ── rename_pool ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_pool_updates_matching_routes(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-a", "coding")
        store.set_pool("sess-b", "main")
        store.set_pool("sess-c", "coding")

        changed = store.rename_pool("coding", "engineering")

        assert changed == 2
        assert store.get_pool("sess-a") == "engineering"
        assert store.get_pool("sess-b") == "main"
        assert store.get_pool("sess-c") == "engineering"
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_rename_pool_no_matches_returns_zero(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("sess-x", "coding")
        changed = store.rename_pool("nonexistent", "other")
        assert changed == 0
        assert store.get_pool("sess-x") == "coding"
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_rename_pool_is_atomic_single_update(tmp_path: Path) -> None:
    """rename_pool must update ALL matching rows in one statement — no
    partial state is observable, and the scope JSON stays consistent with
    pool_name so get_pool does not raise corruption."""
    manager, store = await _open_routing(tmp_path)
    try:
        for i in range(5):
            store.set_pool(f"sess-{i}", "old_pool")

        changed = store.rename_pool("old_pool", "new_pool")

        assert changed == 5
        # Every row must be renamed — no orphans.
        for i in range(5):
            assert store.get_pool(f"sess-{i}") == "new_pool"
        # No row should still reference old_pool.
        assert store.rename_pool("old_pool", "whatever") == 0
    finally:
        store.close()
        await manager.close()


# ── corruption ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_corruption_scope_missing_pool_raises(tmp_path: Path) -> None:
    """A row whose scope lacks a 'pool' field is corruption, not a missing
    route — get_pool must raise, not return None or a default."""
    manager, store = await _open_routing(tmp_path)
    try:
        # Insert a corrupt row directly: scope has no 'pool' key.
        await manager.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) VALUES (?, ?, ?)",
            ("corrupt-1", "pool_a", '{"foo": "bar"}'),
        )
        with pytest.raises(PoolRoutingCorruptionError):
            store.get_pool("corrupt-1")
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_corruption_scope_pool_mismatch_raises(tmp_path: Path) -> None:
    """pool_name and scope.$.pool disagree — corruption."""
    manager, store = await _open_routing(tmp_path)
    try:
        await manager.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) VALUES (?, ?, ?)",
            ("corrupt-2", "pool_a", '{"pool": "pool_b"}'),
        )
        with pytest.raises(PoolRoutingCorruptionError):
            store.get_pool("corrupt-2")
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_corruption_empty_pool_name_raises(tmp_path: Path) -> None:
    manager, store = await _open_routing(tmp_path)
    try:
        await manager.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) VALUES (?, ?, ?)",
            ("corrupt-3", "", '{"pool": ""}'),
        )
        with pytest.raises(PoolRoutingCorruptionError):
            store.get_pool("corrupt-3")
    finally:
        store.close()
        await manager.close()


@pytest.mark.asyncio
async def test_corruption_does_not_affect_valid_rows(tmp_path: Path) -> None:
    """A corrupt row raises on access, but valid rows still work."""
    manager, store = await _open_routing(tmp_path)
    try:
        store.set_pool("valid", "coding")
        await manager.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) VALUES (?, ?, ?)",
            ("corrupt", "x", '{"foo": "bar"}'),
        )

        # Valid row still works.
        assert store.get_pool("valid") == "coding"
        # Corrupt row raises.
        with pytest.raises(PoolRoutingCorruptionError):
            store.get_pool("corrupt")
    finally:
        store.close()
        await manager.close()
