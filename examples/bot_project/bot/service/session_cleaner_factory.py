from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from typing import assert_never

from bot.service.session_gc import SessionCleanerOperations
from modex_agent.core.cleanup import (
    DefaultSessionArtifactCleaner,
    SessionArtifactCleaner,
    SessionCleanupResult,
)
from modex_agent.core.scope import RecordScope
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.persistence.managers import WorkspacePersistenceManager
from modex_agent.workspace.paths import WorkspacePaths


class _CleanerAcquisition(AbstractAsyncContextManager[SessionArtifactCleaner]):
    def __init__(
        self,
        cleaner: SessionArtifactCleaner,
        owned_manager: WorkspacePersistenceManager | None = None,
    ) -> None:
        self._cleaner = cleaner
        self._owned_manager = owned_manager

    async def __aenter__(self) -> SessionArtifactCleaner:
        if self._owned_manager is not None:
            try:
                await self._owned_manager.open()
            except BaseException:
                await self._owned_manager.close()
                raise
        return self._cleaner

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._owned_manager is not None:
            await self._owned_manager.close()


class SessionCleanerFactory(SessionCleanerOperations):
    """Run session cleanup operations with an operation-scoped cleaner."""

    def __init__(
        self,
        *,
        backend: PersistenceBackend,
        persistence_resolver: Callable[[Path], WorkspacePersistenceManager | None],
    ) -> None:
        self._backend = backend
        self._persistence_resolver = persistence_resolver

    async def discover_orphan_scopes(
        self,
        paths: WorkspacePaths,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]:
        async with self._acquire(paths) as cleaner:
            return await cleaner.discover_orphan_scopes(
                live_session_ids=live_session_ids,
                workspace_id=workspace_id,
            )

    async def clean_session_artifacts(
        self,
        paths: WorkspacePaths,
        session_id: str,
        scope: RecordScope,
    ) -> SessionCleanupResult:
        async with self._acquire(paths) as cleaner:
            return await cleaner.clean_session_artifacts(session_id, scope)

    async def __call__(
        self,
        paths: WorkspacePaths,
        session_id: str,
        scope: RecordScope,
    ) -> SessionCleanupResult:
        return await self.clean_session_artifacts(paths, session_id, scope)

    def _acquire(
        self,
        paths: WorkspacePaths,
    ) -> AbstractAsyncContextManager[SessionArtifactCleaner]:
        match self._backend:
            case PersistenceBackend.FILE:
                return _CleanerAcquisition(DefaultSessionArtifactCleaner(paths=paths))
            case PersistenceBackend.SQLITE:
                manager = self._persistence_resolver(paths.root)
                if manager is not None:
                    return _CleanerAcquisition(
                        DefaultSessionArtifactCleaner(
                            paths=paths,
                            database_cleaner=manager.create_session_database_cleaner(),
                        )
                    )
                if not paths.state_db.exists():
                    return _CleanerAcquisition(DefaultSessionArtifactCleaner(paths=paths))
                transient_manager = WorkspacePersistenceManager(paths.state_db)
                return _CleanerAcquisition(
                    DefaultSessionArtifactCleaner(
                        paths=paths,
                        database_cleaner=transient_manager.create_session_database_cleaner(),
                    ),
                    transient_manager,
                )
            case unreachable:
                assert_never(unreachable)
