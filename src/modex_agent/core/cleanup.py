"""Session artifact cleaner ABC and default file implementation.

Defines the :class:`SessionArtifactCleaner` ABC — the single seam for
removing a session's full per-session artifact cascade (DB rows + file
directories).  The business-layer :class:`SessionGarbageCollector` delegates
to this ABC instead of doing cleanup directly.

T17 removes ``fork_contexts`` from the per-session artifact list (10 -> 9),
aligning with T18 which removes fork XML file writing.

The default implementation always removes the nine per-session file units and
optionally delegates structured-row deletion to a storage-specific capability.

The nine artifact units (fork_contexts removed in T17):

1. transcript — ``sessions/<pool>/<safe>.jsonl``
2. index record — ``session_index/<pool>/<safe>.json``
3. memory session dir — ``memory/<pool>/session/<scope>``
4. pruned batches dir — ``memory/<pool>/pruned/<scope>``
5. media uploads dir — ``media/<pool>/uploads/<seg>``
6. runtime trace dir — ``runtime_state/<pool>/trace/<raw_sid>``
7. runtime output dir — ``runtime_state/<pool>/output/<raw_sid>``
8. runtime todos file — ``runtime_state/<pool>/todos/<safe>.json``
9. runtime turn state dir — ``runtime_state/<pool>/turns/<seg_agent>/<seg_sid>``
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_cleanup import (
    MissingSessionScopeError,
    SessionDatabaseCleaner,
    SessionDatabaseCleanupError,
    SessionScopeMismatchError,
)
from modex_agent.core.session_id import agent_of
from modex_agent.core.session_scope_discovery import discover_file_session_scopes
from modex_agent.core.session_store import safe_filename
from modex_agent.memory.stores.utils import sanitize_scope_key
from modex_agent.runtime.store import JsonFileTodoStore, JsonFileTurnStateStore
from modex_agent.workspace.paths import WorkspacePaths, safe_segment

logger = logging.getLogger(__name__)

__all__ = [
    "DefaultSessionArtifactCleaner",
    "SessionArtifactCleaner",
    "SessionCleanupResult",
    "SessionDatabaseCleaner",
    "session_artifact_paths",
]

_UPLOADS_SUBDIR = "uploads"


def session_artifact_paths(session_id: str, pool: str, paths: WorkspacePaths) -> list[Path]:
    """The nine per-session artifact units for *session_id* under *pool*.

    Each entry is a whole per-session directory or file (never a sub-file
    inside a dir), derived with the same on-disk transform its store uses.
    Caller may delete any that exist; all are tolerant of being already
    absent.

    T17 removed ``fork_contexts`` (the ``{agent}_{prefix}.xml`` file) from
    this list, aligning with T18 which removes fork XML file writing.
    """
    agent = agent_of(session_id)
    safe = safe_filename(session_id)
    scope = sanitize_scope_key(session_id)
    seg = safe_segment(session_id)

    return [
        paths.sessions_dir / pool / f"{safe}.jsonl",  # transcript
        paths.session_index_dir / pool / f"{safe}.json",  # index record
        paths.memory_dir(pool) / "session" / scope,  # memory messages
        paths.pruned_dir(pool) / scope,  # pruned batches
        paths.media_dir(pool) / _UPLOADS_SUBDIR / seg,  # media uploads
        paths.runtime_dir(pool, "trace") / session_id,  # trace (raw)
        paths.runtime_dir(pool, "output") / session_id,  # output (raw)
        paths.runtime_dir(pool, "todos")
        / f"{JsonFileTodoStore._safe_segment(session_id)}.json",  # todos
        paths.runtime_dir(pool, "turns")
        / JsonFileTurnStateStore._safe_segment(agent)
        / JsonFileTurnStateStore._safe_segment(session_id),  # turn state
    ]


class SessionCleanupResult(BaseModel):
    """Outcome of cleaning one session's artifacts.

    Frozen Pydantic model: the result is a value object summarising what was
    removed.  ``errors`` collects non-fatal failures (a missing target is NOT
    an error — only unexpected ``OSError`` / DB failures).
    """

    model_config = ConfigDict(frozen=True)

    db_rows_deleted: int = 0
    files_deleted: int = 0
    dirs_deleted: int = 0
    errors: list[str] = Field(default_factory=list)


class SessionArtifactCleaner(ABC):
    """Cleans one session's full artifact cascade (DB rows + file directories).

    The single method :meth:`clean_session_artifacts` is the idempotent unit:
    it removes every per-session artifact (file + DB) for *session_id* under
    *scope*.  Missing targets are no-ops; non-fatal failures are collected in
    the result's ``errors`` list rather than aborting the whole cleanup.

    The business-layer :class:`SessionGarbageCollector` delegates to this ABC
    instead of doing cleanup directly, so file-only and file-plus-database
    backends are interchangeable at the seam.
    """

    @abstractmethod
    async def clean_session_artifacts(
        self, session_id: str, scope: RecordScope
    ) -> SessionCleanupResult:
        """Idempotently remove all per-session artifacts for *session_id*.

        Args:
            session_id: The session whose artifacts should be removed.
            scope: Carries the exact session identity and optional pool,
                workspace, agent, or other isolation dimensions.

        Returns:
            A :class:`SessionCleanupResult` summarising what was removed.
        """
        ...

    @abstractmethod
    async def discover_orphan_scopes(
        self,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]:
        """Discover persisted scopes whose session IDs are not live."""
        ...


class DefaultSessionArtifactCleaner(SessionArtifactCleaner):
    """File cleaner with an optional structured-record cleanup capability.

    Args:
        paths: Workspace paths for the workspace containing the session.
        database_cleaner: Optional storage-specific structured-record cleaner.
    """

    def __init__(
        self,
        *,
        paths: WorkspacePaths,
        database_cleaner: SessionDatabaseCleaner | None = None,
    ) -> None:
        self._paths = paths
        self._database_cleaner = database_cleaner

    async def discover_orphan_scopes(
        self,
        *,
        live_session_ids: frozenset[str],
        workspace_id: str,
    ) -> list[RecordScope]:
        file_scopes = await asyncio.to_thread(
            discover_file_session_scopes,
            self._paths,
            workspace_id,
        )
        database_scopes = (
            await self._database_cleaner.list_session_scopes()
            if self._database_cleaner is not None
            else []
        )
        orphan_scopes = {
            scope.canonical(): scope
            for scope in (*file_scopes, *database_scopes)
            if scope.session_id not in live_session_ids
        }
        return sorted(orphan_scopes.values(), key=RecordScope.canonical)

    async def clean_session_artifacts(
        self, session_id: str, scope: RecordScope
    ) -> SessionCleanupResult:
        scope_session_id = scope.session_id
        if scope_session_id is None:
            raise MissingSessionScopeError
        if scope_session_id != session_id:
            raise SessionScopeMismatchError(session_id, scope_session_id)
        pool = scope.to_path_segment("pool")

        files = 0
        dirs = 0
        db_rows = 0
        errors: list[str] = []

        # --- DB operations (only when connection provided) --------------
        if self._database_cleaner is not None:
            try:
                db_rows = await self._database_cleaner.delete_session_rows(scope)
            except SessionDatabaseCleanupError as exc:
                errors.append(str(exc))

        # --- file operations (always) -----------------------------------
        file_files, file_dirs, file_errors = await asyncio.to_thread(
            self._clean_file_artifacts, session_id, pool
        )
        files += file_files
        dirs += file_dirs
        errors.extend(file_errors)

        return SessionCleanupResult(
            db_rows_deleted=db_rows,
            files_deleted=files,
            dirs_deleted=dirs,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # File cleanup
    # ------------------------------------------------------------------

    def _clean_file_artifacts(self, session_id: str, pool: str) -> tuple[int, int, list[str]]:
        """Remove all nine file artifact units.

        Order is index-first (Path B from ADR-0018): the existence marker
        goes before the transcript, so a mid-cleanup session vanishes from
        the orphan-session rule's view almost immediately.

        Returns:
            ``(files_deleted, dirs_deleted, errors)``
        """
        units = session_artifact_paths(session_id, pool, self._paths)
        errors: list[str] = []
        files = 0
        dirs = 0

        # index record first (existence marker), then transcript, then rest
        index_unit = next(u for u in units if "session_index" in u.parts)
        memory_unit = next(u for u in units if u.parent.name == "session")
        f, d, error = self._remove_unit(index_unit)
        files += f
        dirs += d
        if error is not None:
            errors.append(error)

        transcript_unit = next(u for u in units if u.suffix == ".jsonl")
        f, d, error = self._remove_unit(transcript_unit)
        files += f
        dirs += d
        if error is not None:
            errors.append(error)

        for unit in units:
            if unit in (index_unit, transcript_unit, memory_unit):
                continue
            f, d, error = self._remove_unit(unit)
            files += f
            dirs += d
            if error is not None:
                errors.append(error)

        if not errors:
            f, d, error = self._remove_unit(memory_unit)
            files += f
            dirs += d
            if error is not None:
                errors.append(error)

        return (files, dirs, errors)

    @staticmethod
    def _remove_unit(unit: Path) -> tuple[int, int, str | None]:
        """Remove one unit and return deletion counts plus an optional error.

        ``FileNotFoundError`` is suppressed (idempotent).  Other
        ``OSError`` values (locked file, permission, ...) are logged and
        returned so the caller can retain its sweep retry marker.
        """
        try:
            if unit.is_dir():
                shutil.rmtree(unit)
                return (0, 1, None)
            if unit.exists():
                unit.unlink()
                return (1, 0, None)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("session-cleanup: could not remove %s: %s", unit, exc)
            return (0, 0, f"could not remove {unit}: {exc}")
        return (0, 0, None)
