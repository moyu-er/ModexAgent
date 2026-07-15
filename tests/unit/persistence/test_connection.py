from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from modex_agent.persistence import (
    ConnectionManager,
    DatabaseKind,
    NestedTransactionError,
)


@pytest.mark.asyncio
async def test_open_applies_pragmas_and_reopen_preserves_data(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)

    await manager.open()
    await manager.execute("CREATE TABLE items (value TEXT NOT NULL)")
    await manager.execute("INSERT INTO items (value) VALUES (?)", ("persisted",))
    journal_mode = await manager.query_value("PRAGMA journal_mode", str)
    synchronous = await manager.query_value("PRAGMA synchronous", int)
    foreign_keys = await manager.query_value("PRAGMA foreign_keys", int)
    busy_timeout = await manager.query_value("PRAGMA busy_timeout", int)
    wal_autocheckpoint = await manager.query_value("PRAGMA wal_autocheckpoint", int)
    await manager.close()

    await manager.open()
    value = await manager.query_value("SELECT value FROM items", str)
    await manager.close()

    assert journal_mode == "wal"
    assert synchronous == 1
    assert foreign_keys == 1
    assert busy_timeout == 5000
    assert wal_autocheckpoint == 1000
    assert value == "persisted"


@pytest.mark.asyncio
async def test_transaction_rolls_back_all_statements_on_failure(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    await manager.execute("CREATE TABLE items (value TEXT UNIQUE NOT NULL)")

    with pytest.raises(sqlite3.IntegrityError):
        async with manager.transaction() as transaction:
            await transaction.execute("INSERT INTO items (value) VALUES (?)", ("duplicate",))
            await transaction.execute("INSERT INTO items (value) VALUES (?)", ("duplicate",))

    count = await manager.query_value("SELECT COUNT(*) FROM items", int)
    await manager.close()

    assert count == 0


@pytest.mark.asyncio
async def test_transaction_rejects_nesting_without_deadlocking(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    async with manager.transaction():
        with pytest.raises(NestedTransactionError):
            async with manager.transaction():
                pytest.fail("nested transaction body must not run")

    await manager.close()


@pytest.mark.asyncio
async def test_transaction_requires_operations_through_its_handle(tmp_path: Path) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()

    async with manager.transaction():
        with pytest.raises(NestedTransactionError):
            await manager.execute("SELECT 1")

    await manager.close()


@pytest.mark.asyncio
async def test_reopen_recovers_committed_wal_data_without_prior_close(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    crashed_manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
    recovered_manager = ConnectionManager(db_path, DatabaseKind.WORKSPACE)

    await crashed_manager.open()
    await crashed_manager.execute("CREATE TABLE items (value TEXT NOT NULL)")
    await crashed_manager.execute("INSERT INTO items (value) VALUES (?)", ("durable",))

    await recovered_manager.open()
    value = await recovered_manager.query_value("SELECT value FROM items", str)

    await recovered_manager.close()
    await crashed_manager.close()

    assert value == "durable"
