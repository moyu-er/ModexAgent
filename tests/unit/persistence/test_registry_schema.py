"""Registry DB schema structure tests.

Asserts the target DDL defined in
``src/modex_agent/persistence/migrations/registry/001_initial.sql`` per
ADR-0029 (epoch-ms timestamps + updated_at triggers) and ADR-0031
(workspaces: ms int timestamps, is_home CHECK, metadata_json NOT NULL DEFAULT
'{}'; session_workspace_map: created_at/updated_at columns + trigger).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind

# ---------------------------------------------------------------------------
# Expected physical schema (target state per ADR-0029/0031)
# ---------------------------------------------------------------------------

EXPECTED_TABLES: frozenset[str] = frozenset({"workspaces", "session_workspace_map"})

# Mutable tables that carry `updated_at` + auto-update trigger.
MUTABLE_TABLES_WITH_TRIGGER: frozenset[str] = frozenset({"session_workspace_map"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _open_registry(tmp_path: Path) -> ConnectionManager:
    """Open a registry DB whose packaged 001_initial migration is applied."""
    manager = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
    await manager.open()
    return manager


async def _table_columns(manager: ConnectionManager, table: str) -> list[str]:
    # table_xinfo includes generated columns; table_info may not.
    rows = await manager.query_all(f"PRAGMA table_xinfo({table})")
    return [row[1] for row in rows]


async def _column_type(manager: ConnectionManager, table: str, column: str) -> str | None:
    rows = await manager.query_all(f"PRAGMA table_xinfo({table})")
    for row in rows:
        if row[1] == column:
            return str(row[2])
    return None


async def _trigger_exists(manager: ConnectionManager, trigger_name: str) -> bool:
    count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name = ?",
        int,
        (trigger_name,),
    )
    return count == 1


async def _index_exists(manager: ConnectionManager, index_name: str) -> bool:
    count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = ?",
        int,
        (index_name,),
    )
    return count == 1


# ---------------------------------------------------------------------------
# Table presence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initial_migration_creates_workspaces_and_map_tables(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    table_names = {row[0] for row in rows}
    await manager.close()

    missing = EXPECTED_TABLES - table_names
    assert not missing, f"missing registry tables: {sorted(missing)}"


# ---------------------------------------------------------------------------
# workspaces — int-ms timestamps, is_home CHECK, metadata_json NOT NULL DEFAULT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspaces_timestamps_are_integer_ms(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)
    created_at_type = await _column_type(manager, "workspaces", "created_at")
    last_active_type = await _column_type(manager, "workspaces", "last_active")
    await manager.close()

    assert created_at_type is not None
    assert created_at_type.upper() == "INTEGER", (
        f"workspaces.created_at must be INTEGER, got {created_at_type}"
    )
    assert last_active_type is not None
    assert last_active_type.upper() == "INTEGER", (
        f"workspaces.last_active must be INTEGER, got {last_active_type}"
    )


@pytest.mark.asyncio
async def test_workspaces_is_home_check_constraint(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    # Insert with valid is_home values first.
    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name, is_home) "
        "VALUES (?, ?, ?, ?)",
        ("ws-1", "/path/a", "A", 0),
    )

    # is_home=1 is also valid.
    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name, is_home) "
        "VALUES (?, ?, ?, ?)",
        ("ws-2", "/path/b", "B", 1),
    )

    # is_home=2 must be rejected by CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO workspaces (workspace_id, target_path, display_name, is_home) "
            "VALUES (?, ?, ?, ?)",
            ("ws-3", "/path/c", "C", 2),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_workspaces_metadata_json_not_null_default_empty_object(tmp_path: Path) -> None:
    """metadata_json is NOT NULL with DEFAULT '{}' and a json_valid CHECK."""
    manager = await _open_registry(tmp_path)

    # Omit metadata_json entirely → DEFAULT '{}' fills it.
    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
        ("ws-1", "/path/a", "A"),
    )
    metadata = await manager.query_value(
        "SELECT metadata_json FROM workspaces WHERE workspace_id = ?", str, ("ws-1",)
    )

    # Explicit NULL must be rejected (NOT NULL constraint).
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO workspaces (workspace_id, target_path, display_name, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            ("ws-2", "/path/b", "B", None),
        )

    # Non-JSON value must be rejected by CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO workspaces (workspace_id, target_path, display_name, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            ("ws-3", "/path/c", "C", "not-json"),
        )

    await manager.close()

    assert metadata == "{}"


@pytest.mark.asyncio
async def test_workspaces_target_path_unique(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
        ("ws-1", "/path/a", "A"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        await manager.execute(
            "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
            ("ws-2", "/path/a", "B"),
        )

    await manager.close()


@pytest.mark.asyncio
async def test_workspaces_display_name_nullable(tmp_path: Path) -> None:
    """display_name is nullable (PRD schema summary does not require NOT NULL)."""
    manager = await _open_registry(tmp_path)

    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path) VALUES (?, ?)",
        ("ws-1", "/path/a"),
    )

    row = await manager.query_one(
        "SELECT display_name FROM workspaces WHERE workspace_id = ?", ("ws-1",)
    )
    assert row is not None
    assert row[0] is None

    await manager.close()


# ---------------------------------------------------------------------------
# session_workspace_map — created_at/updated_at + trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_workspace_map_has_created_at_and_updated_at(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)
    cols = set(await _table_columns(manager, "session_workspace_map"))
    await manager.close()

    assert "created_at" in cols, "session_workspace_map must have created_at"
    assert "updated_at" in cols, "session_workspace_map must have updated_at"


@pytest.mark.asyncio
async def test_session_workspace_map_timestamps_are_integer_ms(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)
    created_at_type = await _column_type(manager, "session_workspace_map", "created_at")
    updated_at_type = await _column_type(manager, "session_workspace_map", "updated_at")
    await manager.close()

    assert created_at_type is not None and created_at_type.upper() == "INTEGER"
    assert updated_at_type is not None and updated_at_type.upper() == "INTEGER"


@pytest.mark.asyncio
async def test_session_workspace_map_has_auto_updated_at_trigger(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)
    has_trigger = await _trigger_exists(manager, "trg_session_workspace_map_auto_updated_at")
    await manager.close()

    assert has_trigger, "session_workspace_map must have trg_*_auto_updated_at"


@pytest.mark.asyncio
async def test_workspaces_has_no_trigger(tmp_path: Path) -> None:
    """workspaces has no updated_at column → no trigger (only session_workspace_map does)."""
    manager = await _open_registry(tmp_path)
    rows = await manager.query_all(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
        ("workspaces",),
    )
    found = [row[0] for row in rows]
    await manager.close()

    assert not found, f"workspaces must have no trigger: {found}"


@pytest.mark.asyncio
async def test_session_workspace_map_updated_at_trigger_skips_when_explicit(
    tmp_path: Path,
) -> None:
    """Explicitly SET updated_at → trigger must not override.

    Mirror of the workspace trigger-skip test: backdate updated_at via explicit
    SET, then issue a plain UPDATE — the trigger must advance the backdated
    value. The plain-UPDATE path is what the trigger-skip clause guards against
    being skipped.
    """
    manager = await _open_registry(tmp_path)
    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
        ("ws-1", "/path/a", "A"),
    )
    await manager.execute(
        "INSERT INTO session_workspace_map (session_prefix, workspace_id) VALUES (?, ?)",
        ("sess-1", "ws-1"),
    )

    # Backdate updated_at via explicit SET — trigger's `NEW.updated_at IS OLD.updated_at`
    # is False (NEW is the backdated value, OLD is the INSERT default), so trigger skips.
    backdated = 1
    await manager.execute(
        "UPDATE session_workspace_map SET updated_at = ? WHERE session_prefix = ?",
        (backdated, "sess-1"),
    )
    during = await manager.query_value(
        "SELECT updated_at FROM session_workspace_map WHERE session_prefix = ?",
        int,
        ("sess-1",),
    )
    assert during == backdated, "explicit SET updated_at must survive the trigger"

    # Now a plain UPDATE without updated_at: NEW.updated_at copies OLD.updated_at (backdated)
    # → trigger's WHEN clause is True → trigger fires → updated_at advances.
    await manager.execute(
        "UPDATE session_workspace_map SET workspace_id = ? WHERE session_prefix = ?",
        ("ws-1", "sess-1"),
    )
    after = await manager.query_value(
        "SELECT updated_at FROM session_workspace_map WHERE session_prefix = ?",
        int,
        ("sess-1",),
    )
    await manager.close()

    assert after > backdated, "trigger must advance backdated updated_at when omitted from UPDATE"


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_indexes_present(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    last_active_index = await _index_exists(manager, "idx_workspaces_last_active")
    created_at_index = await _index_exists(manager, "idx_workspaces_created_at")
    workspace_index = await _index_exists(manager, "idx_session_ws_workspace")
    await manager.close()

    assert last_active_index, "idx_workspaces_last_active must exist"
    assert created_at_index, "idx_workspaces_created_at must exist"
    assert workspace_index, "idx_session_ws_workspace must exist"


# ---------------------------------------------------------------------------
# CASCADE behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_workspace_cascades_to_session_map(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)

    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
        ("ws-1", "/path/a", "A"),
    )
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


# ---------------------------------------------------------------------------
# created_at DEFAULT fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspaces_created_at_default_fires_when_omitted(tmp_path: Path) -> None:
    manager = await _open_registry(tmp_path)
    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
        ("ws-1", "/path/a", "A"),
    )
    created_at = await manager.query_value(
        "SELECT created_at FROM workspaces WHERE workspace_id = ?", int, ("ws-1",)
    )
    last_active = await manager.query_value(
        "SELECT last_active FROM workspaces WHERE workspace_id = ?", int, ("ws-1",)
    )
    await manager.close()

    assert isinstance(created_at, int) and created_at > 0
    assert isinstance(last_active, int) and last_active > 0


@pytest.mark.asyncio
async def test_session_workspace_map_timestamps_default_when_omitted(
    tmp_path: Path,
) -> None:
    manager = await _open_registry(tmp_path)
    await manager.execute(
        "INSERT INTO workspaces (workspace_id, target_path, display_name) VALUES (?, ?, ?)",
        ("ws-1", "/path/a", "A"),
    )
    await manager.execute(
        "INSERT INTO session_workspace_map (session_prefix, workspace_id) VALUES (?, ?)",
        ("sess-1", "ws-1"),
    )
    created_at = await manager.query_value(
        "SELECT created_at FROM session_workspace_map WHERE session_prefix = ?",
        int,
        ("sess-1",),
    )
    updated_at = await manager.query_value(
        "SELECT updated_at FROM session_workspace_map WHERE session_prefix = ?",
        int,
        ("sess-1",),
    )
    await manager.close()

    assert isinstance(created_at, int) and created_at > 0
    assert isinstance(updated_at, int) and updated_at > 0
