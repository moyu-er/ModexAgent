from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_agent.persistence import (
    ConnectionManager,
    DatabaseKind,
    MigrationRunner,
    TransactionControlStatementError,
)


def _write_migration(directory: Path, name: str, sql: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(sql, encoding="utf-8")


@pytest.mark.asyncio
async def test_run_pending_is_idempotent(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    _write_migration(
        migration_dir,
        "999_create_items.sql",
        "CREATE TABLE items (value TEXT NOT NULL);",
    )
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    runner = MigrationRunner(manager, DatabaseKind.WORKSPACE, migration_dir=migration_dir)

    await runner.run_pending()
    await runner.run_pending()

    # Packaged 001_initial.sql + test 999_create_items.sql
    version_count = await manager.query_value("SELECT COUNT(*) FROM schema_migrations", int)
    await manager.close()

    assert version_count == 2


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_schema_and_version(tmp_path: Path) -> None:
    migration_dir = tmp_path / "migrations"
    _write_migration(
        migration_dir,
        "999_broken.sql",
        "CREATE TABLE transient_items (value TEXT NOT NULL); INVALID SQL;",
    )
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    runner = MigrationRunner(manager, DatabaseKind.WORKSPACE, migration_dir=migration_dir)

    with pytest.raises(sqlite3.OperationalError):
        await runner.run_pending()

    table_count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'transient_items'",
        int,
    )
    version_count = await manager.query_value("SELECT COUNT(*) FROM schema_migrations", int)
    await manager.close()

    assert table_count == 0
    assert version_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    [
        "BEGIN",
        "COMMIT",
        "ROLLBACK",
        "SAVEPOINT x",
        "RELEASE x",
        "/* migration comment */ BEGIN",
    ],
)
async def test_migration_rejects_transaction_control(tmp_path: Path, statement: str) -> None:
    migration_dir = tmp_path / "migrations"
    _write_migration(
        migration_dir,
        "999_forbidden.sql",
        f"CREATE TABLE items (value TEXT); {statement};",
    )
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    runner = MigrationRunner(manager, DatabaseKind.WORKSPACE, migration_dir=migration_dir)

    with pytest.raises(TransactionControlStatementError):
        await runner.run_pending()

    table_count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'items'",
        int,
    )
    await manager.close()

    assert table_count == 0


@pytest.mark.asyncio
async def test_database_kind_selects_only_its_packaged_stream(tmp_path: Path) -> None:
    workspace_dir = tmp_path / "workspace"
    registry_dir = tmp_path / "registry"
    _write_migration(
        workspace_dir, "999_workspace.sql", "CREATE TABLE workspace_only (id INTEGER);"
    )
    _write_migration(registry_dir, "999_registry.sql", "CREATE TABLE registry_only (id INTEGER);")
    streams = {
        DatabaseKind.WORKSPACE: workspace_dir,
        DatabaseKind.REGISTRY: registry_dir,
    }
    manager = ConnectionManager(tmp_path / "registry.db", DatabaseKind.REGISTRY)
    await manager.open()
    runner = MigrationRunner(
        manager, DatabaseKind.REGISTRY, migration_dir=streams[DatabaseKind.REGISTRY]
    )

    await runner.run_pending()

    registry_count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'registry_only'",
        int,
    )
    workspace_count = await manager.query_value(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'workspace_only'",
        int,
    )
    await manager.close()

    assert registry_count == 1
    assert workspace_count == 0
