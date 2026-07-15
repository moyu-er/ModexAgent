from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind


async def _open_registry(tmp_path: Path) -> ConnectionManager:
    """Open a registry DB whose packaged 001_initial migration is applied."""
    manager = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
    await manager.open()
    return manager


_WORKSPACE_INSERT = (
    "INSERT INTO workspaces (workspace_id, target_path, created_at, last_active) "
    "VALUES (?, ?, ?, ?)"
)
_ROW_ARGS = ("ws-1", "/path/a", "2026-01-01T00:00:00", "2026-01-01T00:00:00")


@pytest.mark.asyncio
async def test_initial_migration_creates_workspaces_and_map_tables(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    workspaces_count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'",
        int,
    )
    map_count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
        "AND name = 'session_workspace_map'",
        int,
    )
    await manager.close()

    assert workspaces_count == 1
    assert map_count == 1


@pytest.mark.asyncio
async def test_target_path_is_unique(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    await manager.execute(_WORKSPACE_INSERT, _ROW_ARGS)
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            _WORKSPACE_INSERT,
            ("ws-2", "/path/a", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_deleting_workspace_cascades_to_session_map(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    await manager.execute(_WORKSPACE_INSERT, _ROW_ARGS)
    await manager.execute(
        "INSERT INTO session_workspace_map (session_prefix, workspace_id) VALUES (?, ?)",
        ("sess-abc", "ws-1"),
    )
    await manager.execute("DELETE FROM workspaces WHERE workspace_id = ?", ("ws-1",))

    remaining = await manager.query_value(
        "SELECT COUNT(*) FROM session_workspace_map WHERE workspace_id = ?",
        int,
        ("ws-1",),
    )
    await manager.close()

    assert remaining == 0


@pytest.mark.asyncio
async def test_indexes_present(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    last_active_index = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_workspaces_last_active'",
        int,
    )
    workspace_index = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
        "AND name = 'idx_session_ws_workspace'",
        int,
    )
    await manager.close()

    assert last_active_index == 1
    assert workspace_index == 1
