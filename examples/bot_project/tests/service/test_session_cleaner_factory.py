from pathlib import Path

import pytest
from bot.service.session_cleaner_factory import SessionCleanerFactory

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_cleanup import SessionScopeMismatchError
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.workspace.paths import WorkspacePaths

from bot.scope import BotRecordScope


def _paths_for(workspace_root: Path) -> WorkspacePaths:
    return WorkspacePaths(root=workspace_root / ".modex")


async def _seed_session(
    manager: WorkspacePersistenceManager,
    scope: RecordScope,
) -> None:
    await manager.connection.execute(
        "INSERT INTO sessions (session_id, scope_key) VALUES (?, ?)",
        (scope.session_id, scope.canonical()),
    )


@pytest.mark.asyncio
async def test_file_backend_cleanup_stays_file_only(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    factory = SessionCleanerFactory(
        backend=PersistenceBackend.FILE,
        persistence_resolver=lambda _root: None,
    )

    result = await factory.clean_session_artifacts(
        paths,
        "missing.main",
        BotRecordScope(pool="main", session_id="missing.main"),
    )

    assert result.db_rows_deleted == 0
    assert not paths.state_db.exists()


@pytest.mark.asyncio
async def test_sqlite_discovery_does_not_create_missing_database(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    factory = SessionCleanerFactory(
        backend=PersistenceBackend.SQLITE,
        persistence_resolver=lambda _root: None,
    )

    scopes = await factory.discover_orphan_scopes(
        paths,
        live_session_ids=frozenset(),
        workspace_id="workspace-1",
    )

    assert scopes == []
    assert not paths.state_db.exists()


@pytest.mark.asyncio
async def test_sqlite_cleanup_borrows_live_manager_without_closing(
    tmp_path: Path,
) -> None:
    paths = _paths_for(tmp_path)
    manager = WorkspacePersistenceManager(paths.state_db)
    await manager.open()
    try:
        scope = BotRecordScope(pool="main", session_id="borrowed.main")
        await _seed_session(manager, scope)
        factory = SessionCleanerFactory(
            backend=PersistenceBackend.SQLITE,
            persistence_resolver=lambda root: manager if root == paths.root else None,
        )

        result = await factory.clean_session_artifacts(paths, "borrowed.main", scope)

        connection_alive = await manager.connection.query_value("SELECT 1", int)
        assert result.db_rows_deleted == 1
        assert connection_alive == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_sqlite_discovery_transiently_opens_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths_for(tmp_path)
    setup_manager = WorkspacePersistenceManager(paths.state_db)
    await setup_manager.open()
    scope = BotRecordScope(pool="main", session_id="inactive.main")
    await _seed_session(setup_manager, scope)
    await setup_manager.close()
    opened_managers: list[WorkspacePersistenceManager] = []
    closed_managers: list[WorkspacePersistenceManager] = []
    original_open = WorkspacePersistenceManager.open
    original_close = WorkspacePersistenceManager.close

    async def _record_open(manager: WorkspacePersistenceManager) -> None:
        opened_managers.append(manager)
        await original_open(manager)

    async def _record_close(manager: WorkspacePersistenceManager) -> None:
        closed_managers.append(manager)
        await original_close(manager)

    monkeypatch.setattr(WorkspacePersistenceManager, "open", _record_open)
    monkeypatch.setattr(WorkspacePersistenceManager, "close", _record_close)
    factory = SessionCleanerFactory(
        backend=PersistenceBackend.SQLITE,
        persistence_resolver=lambda _root: None,
    )

    scopes = await factory.discover_orphan_scopes(
        paths,
        live_session_ids=frozenset(),
        workspace_id="workspace-1",
    )

    # from_canonical returns base RecordScope with pool as an extra attr
    # (extra="allow", ADR-0028 §3) — structurally identical to BotRecordScope.
    # Assert field-value equality, not type identity.
    assert len(scopes) == 1
    assert scopes[0].model_dump() == scope.model_dump()
    assert opened_managers == closed_managers
    assert len(opened_managers) == 1


@pytest.mark.asyncio
async def test_sqlite_discovery_excludes_live_session_scope(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    setup_manager = WorkspacePersistenceManager(paths.state_db)
    await setup_manager.open()
    scope = BotRecordScope(
        pool="main",
        workspace_id="workspace-1",
        session_id="active.main",
        user_id="user-1",
    )
    await _seed_session(setup_manager, scope)
    await setup_manager.close()
    factory = SessionCleanerFactory(
        backend=PersistenceBackend.SQLITE,
        persistence_resolver=lambda _root: None,
    )

    scopes = await factory.discover_orphan_scopes(
        paths,
        live_session_ids=frozenset({"active.main"}),
        workspace_id="workspace-1",
    )

    assert scopes == []


@pytest.mark.asyncio
async def test_transient_manager_closes_when_cleanup_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths_for(tmp_path)
    setup_manager = WorkspacePersistenceManager(paths.state_db)
    await setup_manager.open()
    await setup_manager.close()
    closed_managers: list[WorkspacePersistenceManager] = []
    original_close = WorkspacePersistenceManager.close

    async def _record_close(manager: WorkspacePersistenceManager) -> None:
        closed_managers.append(manager)
        await original_close(manager)

    monkeypatch.setattr(WorkspacePersistenceManager, "close", _record_close)
    factory = SessionCleanerFactory(
        backend=PersistenceBackend.SQLITE,
        persistence_resolver=lambda _root: None,
    )

    with pytest.raises(SessionScopeMismatchError):
        await factory.clean_session_artifacts(
            paths,
            "target.main",
            BotRecordScope(pool="main", session_id="different.main"),
        )

    assert len(closed_managers) == 1


@pytest.mark.asyncio
async def test_transient_manager_closes_when_open_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths_for(tmp_path)
    setup_manager = WorkspacePersistenceManager(paths.state_db)
    await setup_manager.open()
    await setup_manager.close()
    closed_managers: list[WorkspacePersistenceManager] = []
    original_open = WorkspacePersistenceManager.open
    original_close = WorkspacePersistenceManager.close

    async def _fail_after_open(manager: WorkspacePersistenceManager) -> None:
        await original_open(manager)
        raise OSError("open failed")

    async def _record_close(manager: WorkspacePersistenceManager) -> None:
        closed_managers.append(manager)
        await original_close(manager)

    monkeypatch.setattr(WorkspacePersistenceManager, "open", _fail_after_open)
    monkeypatch.setattr(WorkspacePersistenceManager, "close", _record_close)
    factory = SessionCleanerFactory(
        backend=PersistenceBackend.SQLITE,
        persistence_resolver=lambda _root: None,
    )

    with pytest.raises(OSError, match="open failed"):
        await factory.discover_orphan_scopes(
            paths,
            live_session_ids=frozenset(),
            workspace_id="workspace-1",
        )

    assert len(closed_managers) == 1
