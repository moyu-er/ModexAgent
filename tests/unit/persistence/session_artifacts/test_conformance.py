"""FILE/SQLite conformance for DefaultSessionArtifactCleaner (plan §18.2).

Seeds an identical session scope through both persistence backends and
asserts identical deletion, discovery, and idempotency outcomes through
:class:`DefaultSessionArtifactCleaner` — file-only (FILE backend) and
file-plus-database (SQLITE backend with
:class:`SqliteSessionDatabaseCleaner`).
"""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.scope import RecordScope
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.persistence.session_artifacts import (
    DefaultSessionArtifactCleaner,
    SqliteSessionDatabaseCleaner,
    discover_file_session_scopes,
)
from modex_agent.persistence.session_artifacts.cleaner import _session_artifact_paths
from modex_agent.workspace.paths import WorkspacePaths


class _PoolScopedRecordScope(RecordScope):
    """Framework-test-local ``RecordScope`` subclass adding the pool dimension.

    ``BotRecordScope`` lives in the examples layer and cannot be imported by
    framework tests (ADR-0028 layering); this local subclass mirrors its
    ``pool`` field.
    """

    pool: str | None = None


_POOL = "coding"
_SID = "conversation.coding"
_WORKSPACE_ID = "workspace-a"


def _paths_for(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(root=tmp_path / ".modex")


def _seed_file_artifacts(paths: WorkspacePaths) -> None:
    for unit in _session_artifact_paths(_SID, _POOL, paths):
        if unit.suffix in (".json", ".jsonl"):
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("{}", encoding="utf-8")
        else:
            unit.mkdir(parents=True, exist_ok=True)
            (unit / "data").write_text("x", encoding="utf-8")


async def test_file_and_sqlite_cleaners_delete_identically(tmp_path: Path) -> None:
    file_root = tmp_path / "file-ws"
    sqlite_root = tmp_path / "sqlite-ws"
    file_paths = _paths_for(file_root)
    sqlite_paths = _paths_for(sqlite_root)
    _seed_file_artifacts(file_paths)
    _seed_file_artifacts(sqlite_paths)

    manager = WorkspacePersistenceManager(sqlite_paths.state_db)
    await manager.open()
    try:
        sqlite_scope = _PoolScopedRecordScope(
            session_id=_SID, pool=_POOL, workspace_id=_WORKSPACE_ID
        )
        await manager.connection.execute(
            "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
            (_SID, sqlite_scope.canonical()),
        )
        await manager.connection.execute(
            "INSERT INTO memory_kv (scope_key, key, value_json) VALUES (?, 'k', '{}')",
            (sqlite_scope.canonical(),),
        )

        file_cleaner = DefaultSessionArtifactCleaner(paths=file_paths)
        sqlite_cleaner = DefaultSessionArtifactCleaner(
            paths=sqlite_paths,
            database_cleaner=SqliteSessionDatabaseCleaner(manager.connection),
        )
        file_scope = _PoolScopedRecordScope(
            session_id=_SID, pool=_POOL, workspace_id=_WORKSPACE_ID
        )

        file_result = await file_cleaner.clean_session_artifacts(_SID, file_scope)
        sqlite_result = await sqlite_cleaner.clean_session_artifacts(_SID, sqlite_scope)

        assert file_result.files_deleted == sqlite_result.files_deleted
        assert file_result.dirs_deleted == sqlite_result.dirs_deleted
        assert file_result.errors == sqlite_result.errors == []
        assert sqlite_result.db_rows_deleted == 2

        for root_paths in (file_paths, sqlite_paths):
            assert all(
                not unit.exists() for unit in _session_artifact_paths(_SID, _POOL, root_paths)
            )
        remaining_rows = await manager.connection.query_value(
            "SELECT count(*) FROM sessions WHERE session_id = ?",
            int,
            (_SID,),
        )
        assert remaining_rows == 0
    finally:
        await manager.close()


async def test_file_and_sqlite_cleaners_discover_identically(tmp_path: Path) -> None:
    file_paths = _paths_for(tmp_path / "file-ws")
    sqlite_paths = _paths_for(tmp_path / "sqlite-ws")
    for paths in (file_paths, sqlite_paths):
        _seed_file_artifacts(paths)

    manager = WorkspacePersistenceManager(sqlite_paths.state_db)
    await manager.open()
    try:
        db_scope = _PoolScopedRecordScope(
            session_id=_SID, pool=_POOL, workspace_id=_WORKSPACE_ID
        )
        await manager.connection.execute(
            "INSERT INTO memory_kv (scope_key, key, value_json) VALUES (?, 'k', '{}')",
            (db_scope.canonical(),),
        )

        file_discovered = await DefaultSessionArtifactCleaner(
            paths=file_paths
        ).discover_orphan_scopes(
            live_session_ids=frozenset(),
            workspace_id=_WORKSPACE_ID,
        )
        sqlite_discovered = await DefaultSessionArtifactCleaner(
            paths=sqlite_paths,
            database_cleaner=SqliteSessionDatabaseCleaner(manager.connection),
        ).discover_orphan_scopes(
            live_session_ids=frozenset(),
            workspace_id=_WORKSPACE_ID,
        )

        expected_file_scopes = discover_file_session_scopes(
            file_paths, _WORKSPACE_ID
        )
        assert [scope.canonical() for scope in file_discovered] == [
            scope.canonical() for scope in expected_file_scopes
        ]
        # SQLITE discovery is the union: identical file scopes PLUS the
        # DB-persisted pool-stamped scope (canonical-stamped subclass).
        # Compared by canonical key: from_canonical restores whichever
        # structurally identical {pool} subclass registered last, so instance
        # equality across test modules is not stable.
        assert [scope.canonical() for scope in sqlite_discovered] == [
            scope.canonical()
            for scope in sorted(
                [*expected_file_scopes, db_scope],
                key=RecordScope.canonical,
            )
        ]
    finally:
        await manager.close()


async def test_file_and_sqlite_cleaners_are_idempotent(tmp_path: Path) -> None:
    file_paths = _paths_for(tmp_path / "file-ws")
    sqlite_paths = _paths_for(tmp_path / "sqlite-ws")
    _seed_file_artifacts(file_paths)
    _seed_file_artifacts(sqlite_paths)

    manager = WorkspacePersistenceManager(sqlite_paths.state_db)
    await manager.open()
    try:
        sqlite_cleaner = DefaultSessionArtifactCleaner(
            paths=sqlite_paths,
            database_cleaner=SqliteSessionDatabaseCleaner(manager.connection),
        )
        file_cleaner = DefaultSessionArtifactCleaner(paths=file_paths)
        scope = _PoolScopedRecordScope(
            session_id=_SID, pool=_POOL, workspace_id=_WORKSPACE_ID
        )
        file_first = await file_cleaner.clean_session_artifacts(_SID, scope)
        sqlite_first = await sqlite_cleaner.clean_session_artifacts(_SID, scope)
        file_second = await file_cleaner.clean_session_artifacts(_SID, scope)
        sqlite_second = await sqlite_cleaner.clean_session_artifacts(_SID, scope)

        assert file_first.files_deleted == sqlite_first.files_deleted
        assert file_first.dirs_deleted == sqlite_first.dirs_deleted
        assert sqlite_first.db_rows_deleted == 0  # no DB rows were seeded
        assert file_second.files_deleted == file_second.dirs_deleted == 0
        assert sqlite_second.db_rows_deleted == 0
        assert sqlite_second.files_deleted == sqlite_second.dirs_deleted == 0
        assert file_second.errors == sqlite_second.errors == []
    finally:
        await manager.close()
