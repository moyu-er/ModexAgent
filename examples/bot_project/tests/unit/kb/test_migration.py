from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from bot.persistence.migration import BotWorkspaceMigrationRunner
from bot.webui.events import UserMessageEvent
from bot.webui.sqlite_transcript_store import SqliteTranscriptStore

from modex_agent.persistence import ConnectionManager, DatabaseKind


@pytest.fixture
async def migrated_connection(
    tmp_path: Path,
) -> AsyncIterator[ConnectionManager]:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    await BotWorkspaceMigrationRunner(connection).run_pending()
    yield connection
    await connection.close()


async def _names(
    connection: ConnectionManager,
    object_type: str,
) -> set[str]:
    rows = await connection.query_all(
        "SELECT name FROM sqlite_master WHERE type = ?",
        (object_type,),
    )
    return {str(row[0]) for row in rows}


async def test_kb_entries_table_exists_after_migration(
    migrated_connection: ConnectionManager,
) -> None:
    tables = await _names(migrated_connection, "table")
    assert "kb_entries" in tables


async def test_kb_entries_fts_virtual_table_exists(
    migrated_connection: ConnectionManager,
) -> None:
    tables = await _names(migrated_connection, "table")
    assert "kb_entries_fts" in tables


async def test_fts5_sync_triggers_exist(
    migrated_connection: ConnectionManager,
) -> None:
    triggers = await _names(migrated_connection, "trigger")
    assert "kb_fts_insert" in triggers
    assert "kb_fts_delete" in triggers
    assert "kb_fts_update" in triggers


async def test_kb_indexes_exist(
    migrated_connection: ConnectionManager,
) -> None:
    indexes = await _names(migrated_connection, "index")
    assert "idx_kb_entries_task" in indexes
    assert "idx_kb_entries_category" in indexes


async def test_kb_unique_key_is_scoped_by_task_and_session(
    migrated_connection: ConnectionManager,
) -> None:
    insert_sql = (
        "INSERT INTO kb_entries "
        "(entry_id, key, value, task_id, session_id, created_at, updated_at) "
        "VALUES (?, 'shared', 'value', 'task1', ?, 1, 1)"
    )
    await migrated_connection.execute(insert_sql, (1, "session1"))
    await migrated_connection.execute(insert_sql, (2, "session2"))

    with pytest.raises(sqlite3.IntegrityError):
        await migrated_connection.execute(insert_sql, (3, "session1"))


async def test_run_pending_twice_is_idempotent(
    tmp_path: Path,
) -> None:
    connection = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await connection.open()
    runner = BotWorkspaceMigrationRunner(connection)
    await runner.run_pending()
    await runner.run_pending()
    tables = await _names(connection, "table")
    assert "kb_entries" in tables
    assert "bot_webui_transcript_events" in tables
    await connection.close()


async def test_transcript_table_still_works_after_kb_migration(
    migrated_connection: ConnectionManager,
) -> None:
    store = SqliteTranscriptStore(migrated_connection)
    await store.append(
        "conv.main",
        UserMessageEvent(
            session_id="conv.main",
            agent_name="main",
            content="hello",
            timestamp=100,
        ),
        pool="main",
    )
    events = await store.load("conv.main")
    assert len(events) == 1
    assert events[0].to_dict().get("content") == "hello"
