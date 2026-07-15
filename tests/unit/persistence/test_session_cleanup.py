from __future__ import annotations

from pathlib import Path
from sqlite3 import IntegrityError

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_cleanup import (
    MissingSessionScopeError,
    SessionDatabaseCleanupError,
)
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.persistence.session_cleanup import SqliteSessionDatabaseCleaner

_TARGET = "conversation.main"
_SIBLING = "conversation.worker"

_SCOPE_KEY_TABLES = (
    "memory_session_messages",
    "memory_kv",
    "memory_cursors",
    "memory_revisions",
    "memory_archive_state",
    "memory_archive_entries",
)
_SCOPE_TABLES = (
    "sessions",
    "turn_snapshots",
    "approval_audit_log",
    "todos",
    "external_session_map",
)
_MULTI_ROW_SCOPE_TABLES = ("turn_snapshots", "approval_audit_log")
_INBOX_CHILD_TABLES = (
    "inbox_messages",
    "inbox_delivered_ids",
    "inbox_dead_letter",
)


async def _seed_exact_scope_rows(
    manager: WorkspacePersistenceManager,
    scope: RecordScope,
    *,
    include_session_keyed: bool = True,
) -> None:
    session_id = scope.session_id
    if session_id is None:
        raise AssertionError("test fixture requires a session scope")
    scope_key = scope.canonical()
    owner_scope_key = RecordScope(pool="inbox-owner").canonical()
    connection = manager.connection

    if include_session_keyed:
        await connection.execute(
            "INSERT INTO sessions (session_id, scope) VALUES (?, ?)",
            (session_id, scope_key),
        )
    await connection.execute(
        "INSERT INTO inbox_topics "
        "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
        "VALUES (?, ?, ?, ?, 1, 1)",
        (owner_scope_key, scope_key, session_id, scope_key),
    )
    topic_id = await connection.query_value(
        "SELECT topic_id FROM inbox_topics WHERE scope_key = ?",
        int,
        (scope_key,),
    )
    await connection.execute(
        "INSERT INTO inbox_messages "
        "(topic_id, owner_scope_key, scope_key, session_id, scope, message_id, "
        "message_type, source_name, content, seq, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'agent_message', 'main', 'x', 1, 1)",
        (topic_id, owner_scope_key, scope_key, session_id, scope_key, f"message-{scope_key}"),
    )
    await connection.execute(
        "INSERT INTO inbox_delivered_ids "
        "(owner_scope_key, scope_key, session_id, message_id, delivered_at) "
        "VALUES (?, ?, ?, ?, 1)",
        (owner_scope_key, scope_key, session_id, f"delivered-{scope_key}"),
    )
    await connection.execute(
        "INSERT INTO inbox_dead_letter "
        "(owner_scope_key, scope_key, session_id, scope, message_id, message_type, "
        "source_name, content, expired_reason, expired_at, original_created_at) "
        "VALUES (?, ?, ?, ?, ?, 'agent_message', 'main', 'x', 'expired', 1, 1)",
        (owner_scope_key, scope_key, session_id, scope_key, f"dead-{scope_key}"),
    )
    await connection.execute(
        "INSERT INTO turn_snapshots "
        "(session_id, agent_id, turn_id, scope, phase, created_at, updated_at, payload_json) "
        "VALUES (?, 'main', ?, ?, 'completed', 1, 1, '{}')",
        (session_id, f"turn-{scope_key}", scope_key),
    )
    await connection.execute(
        "INSERT INTO approval_audit_log "
        "(turn_uuid, session_id, scope, agent_id, turn_id, tool_name, decision, decided_at) "
        "VALUES (?, ?, ?, 'main', ?, 'bash', 'approved', 1)",
        (f"uuid-{scope_key}", session_id, scope_key, f"turn-{scope_key}"),
    )
    if include_session_keyed:
        await connection.execute(
            "INSERT INTO todos (session_id, scope, items_json, updated_at) VALUES (?, ?, '[]', 1)",
            (session_id, scope_key),
        )
    await connection.execute(
        "INSERT INTO memory_session_messages "
        "(scope_key, scope, seq, role, message_json, created_at) "
        "VALUES (?, ?, 1, 'user', '{}', 1)",
        (scope_key, scope_key),
    )
    await connection.execute(
        "INSERT INTO memory_kv (scope_key, key, scope, value_json, updated_at) "
        "VALUES (?, 'key', ?, '{}', 1)",
        (scope_key, scope_key),
    )
    await connection.execute(
        "INSERT INTO memory_cursors (scope_key, cursor_name, scope, cursor_value, updated_at) "
        "VALUES (?, 'cursor', ?, 1, 1)",
        (scope_key, scope_key),
    )
    await connection.execute(
        "INSERT INTO memory_revisions (scope_key, scope, message_count, version, updated_at) "
        "VALUES (?, ?, 1, 1, 1)",
        (scope_key, scope_key),
    )
    await connection.execute(
        "INSERT INTO memory_archive_state (scope_key, scope, updated_at) VALUES (?, ?, 1)",
        (scope_key, scope_key),
    )
    await connection.execute(
        "INSERT INTO memory_archive_entries "
        "(scope_key, scope, archive_id, channel, created_at) "
        "VALUES (?, ?, 1, 'summary', 1)",
        (scope_key, scope_key),
    )
    if include_session_keyed:
        await connection.execute(
            "INSERT INTO external_session_map "
            "(modex_session_id, provider_session_id, provider_kind, scope, last_committed_at) "
            "VALUES (?, ?, 'pi', ?, 1)",
            (session_id, f"provider-{session_id}", scope_key),
        )


async def _seed_memory_row(
    manager: WorkspacePersistenceManager,
    scope: RecordScope,
    key: str,
) -> None:
    scope_key = scope.canonical()
    await manager.connection.execute(
        "INSERT INTO memory_kv (scope_key, key, scope, value_json, updated_at) "
        "VALUES (?, ?, ?, '{}', 1)",
        (scope_key, key, scope_key),
    )


async def _count_exact(
    manager: WorkspacePersistenceManager,
    table: str,
    column: str,
    scope: RecordScope,
) -> int:
    return await manager.connection.query_value(
        f"SELECT count(*) FROM {table} WHERE {column} = ?",
        int,
        (scope.canonical(),),
    )


@pytest.mark.asyncio
async def test_sqlite_cleaner_deletes_only_the_exact_poolless_scope(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        target = RecordScope(session_id=_TARGET)
        await _seed_exact_scope_rows(manager, target)
        await _seed_exact_scope_rows(manager, RecordScope(session_id=_SIBLING))
        survivors = (
            RecordScope(session_id=_TARGET, pool="main"),
            RecordScope(session_id=_TARGET, workspace_id="workspace-a"),
            RecordScope(session_id=_TARGET, user_id="user-a"),
            RecordScope(session_id=f"{_TARGET}.child"),
        )
        for survivor in survivors:
            await _seed_exact_scope_rows(
                manager,
                survivor,
                include_session_keyed=False,
            )
        shared = RecordScope(pool="main", user_id="shared")
        await _seed_memory_row(manager, shared, "shared")
        await manager.connection.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) "
            "VALUES ('conversation', 'main', ?)",
            (RecordScope(pool="main").canonical(),),
        )

        deleted = await SqliteSessionDatabaseCleaner(manager.connection).delete_session_rows(target)

        assert deleted == 15
        for table in _SCOPE_KEY_TABLES:
            assert await _count_exact(manager, table, "scope_key", target) == 0
        for table in _SCOPE_TABLES:
            assert await _count_exact(manager, table, "scope", target) == 0
        assert await _count_exact(manager, "inbox_topics", "scope_key", target) == 0
        for table in _INBOX_CHILD_TABLES:
            assert await _count_exact(manager, table, "scope_key", target) == 0
        for survivor in survivors:
            for table in _SCOPE_KEY_TABLES:
                assert await _count_exact(manager, table, "scope_key", survivor) == 1
            for table in _MULTI_ROW_SCOPE_TABLES:
                assert await _count_exact(manager, table, "scope", survivor) == 1
            assert await _count_exact(manager, "inbox_topics", "scope_key", survivor) == 1
            for table in _INBOX_CHILD_TABLES:
                assert await _count_exact(manager, table, "scope_key", survivor) == 1
        assert await _count_exact(manager, "memory_kv", "scope_key", shared) == 1
        assert (
            await manager.connection.query_value(
                "SELECT count(*) FROM pool_routing WHERE session_prefix = 'conversation'",
                int,
            )
            == 1
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_is_idempotent_and_keeps_borrowed_connection_open(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        scope = RecordScope(pool="main", session_id=_TARGET)
        await _seed_exact_scope_rows(manager, scope)
        cleaner = SqliteSessionDatabaseCleaner(manager.connection)

        assert await cleaner.delete_session_rows(scope) == 15
        assert await cleaner.delete_session_rows(scope) == 0
        assert await manager.connection.query_value("SELECT 1", int) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_rejects_missing_session_before_database_access(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    cleaner = SqliteSessionDatabaseCleaner(manager.connection)

    with pytest.raises(MissingSessionScopeError, match="requires session_id"):
        await cleaner.delete_session_rows(RecordScope(pool="main"))


@pytest.mark.asyncio
async def test_sqlite_cleaner_rolls_back_all_exact_scope_deletes(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        scope = RecordScope(pool="main", session_id=_TARGET)
        await _seed_exact_scope_rows(manager, scope)
        await manager.connection.execute(
            "CREATE TRIGGER block_todo_cleanup BEFORE DELETE ON todos "
            "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
        )

        with pytest.raises(
            SessionDatabaseCleanupError,
            match="session database cleanup failed",
        ) as exc_info:
            await SqliteSessionDatabaseCleaner(manager.connection).delete_session_rows(scope)

        assert exc_info.value.scope == scope
        assert isinstance(exc_info.value.__cause__, IntegrityError)
        assert "blocked" not in str(exc_info.value)

        for table in _SCOPE_KEY_TABLES:
            assert await _count_exact(manager, table, "scope_key", scope) == 1
        for table in _SCOPE_TABLES:
            assert await _count_exact(manager, table, "scope", scope) == 1
        assert await _count_exact(manager, "inbox_topics", "scope_key", scope) == 1
        for table in _INBOX_CHILD_TABLES:
            assert await _count_exact(manager, table, "scope_key", scope) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_lists_complete_exact_session_scopes(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        detailed = RecordScope(
            pool="coding",
            workspace_id="workspace-a",
            session_id=_TARGET,
            session_prefix="conversation",
            agent_id="main",
            agent_role="primary",
            user_id="user-a",
            tenant_id="tenant-a",
            channel="web",
            chat_id="chat-a",
            invocation_id="invocation-a",
            parent_session_id="conversation.parent",
        )
        poolless = RecordScope(session_id=_SIBLING, workspace_id="workspace-a")
        await _seed_exact_scope_rows(manager, detailed)
        await _seed_memory_row(manager, detailed, "duplicate")
        await _seed_memory_row(manager, poolless, "poolless")

        scopes = await SqliteSessionDatabaseCleaner(manager.connection).list_session_scopes()

        assert scopes == sorted(
            [detailed, poolless],
            key=RecordScope.canonical,
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_filters_session_scopes_by_exact_session_id(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        poolless = RecordScope(session_id=_TARGET, workspace_id="workspace-a")
        pooled = RecordScope(
            session_id=_TARGET,
            pool="coding",
            workspace_id="workspace-a",
            user_id="user-a",
        )
        partial_name = RecordScope(session_id=f"{_TARGET}.child", pool="coding")
        sibling = RecordScope(session_id=_SIBLING, pool="coding")
        for index, scope in enumerate((poolless, pooled, partial_name, sibling)):
            await _seed_memory_row(manager, scope, f"scope-{index}")

        scopes = await SqliteSessionDatabaseCleaner(manager.connection).list_session_scopes(
            frozenset({_TARGET})
        )

        assert scopes == sorted(
            [poolless, pooled],
            key=RecordScope.canonical,
        )
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_skips_valid_non_session_scope(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        non_session_scope = RecordScope(pool="coding")
        await manager.connection.execute(
            "INSERT INTO memory_kv (scope_key, key, scope, value_json, updated_at) "
            "VALUES (?, 'key', ?, '{}', 1)",
            (non_session_scope.canonical(), non_session_scope.canonical()),
        )

        scopes = await SqliteSessionDatabaseCleaner(manager.connection).list_session_scopes()

        assert scopes == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_skips_malformed_persisted_scope(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        await manager.connection.execute(
            "INSERT INTO memory_kv (scope_key, key, scope, value_json, updated_at) "
            "VALUES ('not-json', 'key', '{\"pool\":\"coding\"}', '{}', 1)"
        )

        # Malformed scope keys are skipped (not fatal) so discovery is resilient.
        result = await SqliteSessionDatabaseCleaner(manager.connection).list_session_scopes()
        assert result == []
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_canonicalizes_noncanonical_persisted_scope_key(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        scope = RecordScope(pool="coding", session_id=_TARGET)
        noncanonical_key = '{"session_id":"conversation.main","pool":"coding"}'
        assert noncanonical_key != scope.canonical()
        await manager.connection.execute(
            "INSERT INTO memory_kv (scope_key, key, scope, value_json, updated_at) "
            "VALUES (?, 'key', ?, '{}', 1)",
            (noncanonical_key, scope.canonical()),
        )

        # Non-canonical scope keys are canonicalized (not rejected) so
        # pre-existing data with different JSON formatting is still discovered.
        result = await SqliteSessionDatabaseCleaner(manager.connection).list_session_scopes()
        assert len(result) == 1
        assert result[0].session_id == _TARGET
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_cleaner_translates_native_scope_discovery_failure(
    tmp_path: Path,
) -> None:
    manager = WorkspacePersistenceManager(tmp_path / "state.db")
    await manager.open()
    try:
        await manager.connection.execute("DROP TABLE memory_kv")

        with pytest.raises(
            SessionDatabaseCleanupError,
            match="session database cleanup failed",
        ) as exc_info:
            await SqliteSessionDatabaseCleaner(manager.connection).list_session_scopes()

        assert exc_info.value.scope is None
    finally:
        await manager.close()
