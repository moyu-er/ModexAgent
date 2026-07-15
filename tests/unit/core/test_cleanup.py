"""TDD tests for SessionArtifactCleaner ABC + SessionCleanupResult (T17).

Tests the seam:

- :class:`SessionCleanupResult` — frozen Pydantic model summarising what was
  removed (``db_rows_deleted``, ``files_deleted``, ``dirs_deleted``, ``errors``).
- :class:`SessionArtifactCleaner` — ABC with one method
  ``clean_session_artifacts(session_id, scope) -> SessionCleanupResult``.
- :func:`session_artifact_paths` — the nine per-session artifact path units
  (fork_contexts removed in T17, aligning with T18).
- :class:`DefaultSessionArtifactCleaner` — file-only mode (current) and the
  structural stub for file+DB mode (future, T20-T25).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.core.cleanup import (
    DefaultSessionArtifactCleaner,
    SessionArtifactCleaner,
    SessionCleanupResult,
    session_artifact_paths,
)
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_cleanup import (
    MissingSessionScopeError,
    SessionDatabaseCleaner,
    SessionDatabaseCleanupError,
    SessionScopeMismatchError,
)
from modex_agent.workspace.paths import WorkspacePaths

# ---------------------------------------------------------------------------
# SessionCleanupResult
# ---------------------------------------------------------------------------


def test_cleanup_result_defaults_to_zero() -> None:
    result = SessionCleanupResult()
    assert result.db_rows_deleted == 0
    assert result.files_deleted == 0
    assert result.dirs_deleted == 0
    assert result.errors == []


def test_cleanup_result_is_frozen() -> None:
    result = SessionCleanupResult(files_deleted=3)
    with pytest.raises(ValidationError):
        result.files_deleted = 5  # type: ignore[misc]


def test_cleanup_result_accepts_all_fields() -> None:
    result = SessionCleanupResult(
        db_rows_deleted=6,
        files_deleted=2,
        dirs_deleted=4,
        errors=["boom"],
    )
    assert result.db_rows_deleted == 6
    assert result.files_deleted == 2
    assert result.dirs_deleted == 4
    assert result.errors == ["boom"]


# ---------------------------------------------------------------------------
# SessionArtifactCleaner ABC
# ---------------------------------------------------------------------------


def test_cleaner_abc_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        SessionArtifactCleaner()  # type: ignore[abstract]


def test_cleaner_abc_subclass_must_implement_method() -> None:
    class _Incomplete(SessionArtifactCleaner):
        pass

    with pytest.raises(TypeError):
        _Incomplete()  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# session_artifact_paths — nine units, fork_contexts removed
# ---------------------------------------------------------------------------


def _paths_for(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(root=tmp_path / ".modex")


def test_artifact_paths_returns_exactly_nine(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    ap = session_artifact_paths("009fc886ecba.coding", "coding", paths)
    assert len(ap) == 9


def test_artifact_paths_excludes_fork_contexts(tmp_path: Path) -> None:
    """T17 removes fork_contexts from the artifact list (10 -> 9).

    Aligns with T18 which removes fork XML file writing.
    """
    paths = _paths_for(tmp_path)
    ap = session_artifact_paths("009fc886ecba.coding", "coding", paths)
    assert not any("fork_contexts" in str(p) for p in ap)


def test_artifact_paths_correct_naming(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    sid = "009fc886ecba.coding"
    pool = "coding"
    ap = session_artifact_paths(sid, pool, paths)

    # transcript + index: safe_filename (dot kept), pool-partitioned
    assert (paths.sessions_dir / pool / "009fc886ecba.coding.jsonl") in ap
    assert (paths.session_index_dir / pool / "009fc886ecba.coding.json") in ap
    # memory session + pruned: sanitize_scope_key (dot kept)
    assert (paths.memory_dir(pool) / "session" / "009fc886ecba.coding") in ap
    assert (paths.pruned_dir(pool) / "009fc886ecba.coding") in ap
    # media uploads: safe_segment (dot -> _)
    assert (paths.media_dir(pool) / "uploads" / "009fc886ecba_coding") in ap
    # runtime trace + output: raw sid
    assert (paths.runtime_dir(pool, "trace") / "009fc886ecba.coding") in ap
    assert (paths.runtime_dir(pool, "output") / "009fc886ecba.coding") in ap
    # todos: dot-preserving -> sid.json
    assert (paths.runtime_dir(pool, "todos") / "009fc886ecba.coding.json") in ap
    # turns: hash-suffix segment under agent/session dirs
    from modex_agent.runtime.store import JsonFileTurnStateStore

    seg_agent = JsonFileTurnStateStore._safe_segment("coding")
    seg_sid = JsonFileTurnStateStore._safe_segment(sid)
    assert (paths.runtime_dir(pool, "turns") / seg_agent / seg_sid) in ap


def test_artifact_paths_excludes_pool_shared(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    ap = session_artifact_paths("x.main", "main", paths)
    # archive + knowledge are pool-shared, must NOT appear
    assert not any("archive" in str(p) for p in ap)
    assert not any("knowledge" in str(p) for p in ap)
    # commands leaf is unused, must NOT appear
    assert not any("commands" in str(p) for p in ap)


# ---------------------------------------------------------------------------
# DefaultSessionArtifactCleaner — file-only mode
# ---------------------------------------------------------------------------


def _write_index(
    paths: WorkspacePaths, pool: str, session_id: str, parent: str | None = None
) -> None:
    rec = {
        "session_id": session_id,
        "agent_name": session_id.split(".")[-1],
        "parent_session_id": parent,
        "created_at": 0,
        "updated_at": 0,
        "metadata": {},
    }
    d = paths.session_index_dir / pool
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _seed_full_session(
    paths: WorkspacePaths, pool: str, sid: str, parent: str | None = None
) -> None:
    _write_index(paths, pool, sid, parent)
    for unit in session_artifact_paths(sid, pool, paths):
        if unit.suffix == ".json" and "session_index" in str(unit):
            continue
        if unit.suffix in (".json", ".jsonl"):
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("{}", encoding="utf-8")
        else:
            unit.mkdir(parents=True, exist_ok=True)
            (unit / "data").write_text("x", encoding="utf-8")


def test_cleaner_removes_all_nine_units(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    pool = "coding"
    sid = "aaa.coding"
    _seed_full_session(paths, pool, sid)

    cleaner = DefaultSessionArtifactCleaner(paths=paths)
    scope = RecordScope(session_id=sid, pool=pool)
    result = asyncio.run(cleaner.clean_session_artifacts(sid, scope))

    for unit in session_artifact_paths(sid, pool, paths):
        assert not unit.exists(), f"still present: {unit}"
    assert result.db_rows_deleted == 0  # file-only mode
    assert result.files_deleted + result.dirs_deleted == 9
    assert result.errors == []


def test_cleaner_idempotent_when_already_gone(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    cleaner = DefaultSessionArtifactCleaner(paths=paths)
    scope = RecordScope(session_id="ghost.coding", pool="coding")

    result = asyncio.run(cleaner.clean_session_artifacts("ghost.coding", scope))
    assert result.files_deleted == 0
    assert result.dirs_deleted == 0
    assert result.errors == []


def test_cleaner_preserves_pool_shared_and_siblings(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    pool = "coding"
    _seed_full_session(paths, pool, "aaa.coding")
    _seed_full_session(paths, pool, "sib.coding")
    archive = paths.memory_dir(pool) / "archive"
    archive.mkdir(parents=True)
    (archive / "state.json").write_text("{}", encoding="utf-8")
    knowledge = paths.memory_dir(pool) / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "MEMORY.md").write_text("kept", encoding="utf-8")

    cleaner = DefaultSessionArtifactCleaner(paths=paths)
    scope = RecordScope(session_id="aaa.coding", pool=pool)
    asyncio.run(cleaner.clean_session_artifacts("aaa.coding", scope))

    # sibling untouched
    assert (paths.session_index_dir / pool / "sib.coding.json").exists()
    # pool-shared untouched
    assert (archive / "state.json").exists()
    assert (knowledge / "MEMORY.md").read_text(encoding="utf-8") == "kept"


def test_cleaner_uses_default_pool_path_when_scope_has_no_pool(
    tmp_path: Path,
) -> None:
    paths = _paths_for(tmp_path)
    session_id = "aaa.coding"
    default_pool = RecordScope().to_path_segment("pool")
    _seed_full_session(paths, default_pool, session_id)
    cleaner = DefaultSessionArtifactCleaner(paths=paths)
    scope = RecordScope(session_id=session_id)

    result = asyncio.run(cleaner.clean_session_artifacts(session_id, scope))

    assert result.files_deleted + result.dirs_deleted == 9
    assert result.errors == []
    assert all(
        not unit.exists() for unit in session_artifact_paths(session_id, default_pool, paths)
    )


def test_cleaner_rejects_session_scope_mismatch_before_database_or_files(
    tmp_path: Path,
) -> None:
    class _RecordingDatabaseCleaner(SessionDatabaseCleaner):
        def __init__(self) -> None:
            self.called = False

        async def delete_session_rows(self, scope: RecordScope) -> int:
            self.called = True
            return 0

        async def list_session_scopes(
            self, session_ids: frozenset[str] | None = None
        ) -> list[RecordScope]:
            return []

    paths = _paths_for(tmp_path)
    argument_session = "argument.coding"
    scoped_session = "scope.coding"
    pool = "coding"
    _seed_full_session(paths, pool, argument_session)
    _seed_full_session(paths, pool, scoped_session)
    database_cleaner = _RecordingDatabaseCleaner()
    cleaner = DefaultSessionArtifactCleaner(
        paths=paths,
        database_cleaner=database_cleaner,
    )

    with pytest.raises(SessionScopeMismatchError, match="does not match"):
        asyncio.run(
            cleaner.clean_session_artifacts(
                argument_session,
                RecordScope(session_id=scoped_session, pool=pool),
            )
        )

    assert database_cleaner.called is False
    assert all(
        unit.exists()
        for session_id in (argument_session, scoped_session)
        for unit in session_artifact_paths(session_id, pool, paths)
    )


def test_cleaner_rejects_missing_scope_session_before_database_or_files(
    tmp_path: Path,
) -> None:
    class _RecordingDatabaseCleaner(SessionDatabaseCleaner):
        def __init__(self) -> None:
            self.called = False

        async def delete_session_rows(self, scope: RecordScope) -> int:
            self.called = True
            return 0

        async def list_session_scopes(
            self, session_ids: frozenset[str] | None = None
        ) -> list[RecordScope]:
            return []

    paths = _paths_for(tmp_path)
    session_id = "argument.coding"
    pool = "coding"
    _seed_full_session(paths, pool, session_id)
    database_cleaner = _RecordingDatabaseCleaner()
    cleaner = DefaultSessionArtifactCleaner(
        paths=paths,
        database_cleaner=database_cleaner,
    )

    with pytest.raises(MissingSessionScopeError, match="requires session_id"):
        asyncio.run(
            cleaner.clean_session_artifacts(
                session_id,
                RecordScope(pool=pool),
            )
        )

    assert database_cleaner.called is False
    assert all(unit.exists() for unit in session_artifact_paths(session_id, pool, paths))


def test_cleaner_accepts_optional_database_cleaner(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    pool = "coding"
    sid = "aaa.coding"
    _seed_full_session(paths, pool, sid)

    cleaner = DefaultSessionArtifactCleaner(paths=paths, database_cleaner=None)
    scope = RecordScope(session_id=sid, pool=pool)
    result = asyncio.run(cleaner.clean_session_artifacts(sid, scope))

    assert result.db_rows_deleted == 0
    assert result.errors == []


def test_cleaner_delegates_structured_row_cleanup(tmp_path: Path) -> None:
    class _DatabaseCleaner(SessionDatabaseCleaner):
        async def delete_session_rows(self, scope: RecordScope) -> int:
            assert scope == RecordScope(session_id="aaa.coding", pool="coding")
            return 4

        async def list_session_scopes(
            self, session_ids: frozenset[str] | None = None
        ) -> list[RecordScope]:
            return []

    paths = _paths_for(tmp_path)
    cleaner = DefaultSessionArtifactCleaner(
        paths=paths,
        database_cleaner=_DatabaseCleaner(),
    )
    scope = RecordScope(session_id="aaa.coding", pool="coding")

    result = asyncio.run(cleaner.clean_session_artifacts("aaa.coding", scope))

    assert result.db_rows_deleted == 4


def test_cleaner_records_database_failure_and_still_cleans_files(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    session_id = "aaa.coding"
    pool = "coding"
    _seed_full_session(paths, pool, session_id)
    index_path = paths.session_index_dir / pool / f"{session_id}.json"

    class _FailingDatabaseCleaner(SessionDatabaseCleaner):
        async def delete_session_rows(self, scope: RecordScope) -> int:
            assert index_path.exists()
            raise SessionDatabaseCleanupError(scope=scope)

        async def list_session_scopes(
            self, session_ids: frozenset[str] | None = None
        ) -> list[RecordScope]:
            return []

    cleaner = DefaultSessionArtifactCleaner(
        paths=paths,
        database_cleaner=_FailingDatabaseCleaner(),
    )

    result = asyncio.run(
        cleaner.clean_session_artifacts(
            session_id,
            RecordScope(session_id=session_id, pool=pool),
        )
    )

    assert result.db_rows_deleted == 0
    assert result.errors == ["session database cleanup failed"]
    assert result.files_deleted + result.dirs_deleted == 9
    assert all(not unit.exists() for unit in session_artifact_paths(session_id, pool, paths))


def test_cleaner_does_not_swallow_unrelated_database_failure(tmp_path: Path) -> None:
    paths = _paths_for(tmp_path)
    session_id = "aaa.coding"
    pool = "coding"
    _seed_full_session(paths, pool, session_id)

    class _BrokenDatabaseCleaner(SessionDatabaseCleaner):
        async def delete_session_rows(self, scope: RecordScope) -> int:
            raise RuntimeError(scope.session_id)

        async def list_session_scopes(
            self, session_ids: frozenset[str] | None = None
        ) -> list[RecordScope]:
            return []

    cleaner = DefaultSessionArtifactCleaner(
        paths=paths,
        database_cleaner=_BrokenDatabaseCleaner(),
    )

    with pytest.raises(RuntimeError, match=session_id):
        asyncio.run(
            cleaner.clean_session_artifacts(
                session_id,
                RecordScope(session_id=session_id, pool=pool),
            )
        )

    assert all(unit.exists() for unit in session_artifact_paths(session_id, pool, paths))


def test_cleaner_retains_memory_marker_when_file_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths_for(tmp_path)
    session_id = "aaa.coding"
    pool = "coding"
    _seed_full_session(paths, pool, session_id)
    trace_dir = paths.runtime_dir(pool, "trace") / session_id
    original_rmtree = shutil.rmtree

    def _fail_trace(path: Path) -> None:
        if path == trace_dir:
            raise OSError("locked trace")
        original_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", _fail_trace)

    result = asyncio.run(
        DefaultSessionArtifactCleaner(paths=paths).clean_session_artifacts(
            session_id,
            RecordScope(session_id=session_id, pool=pool),
        )
    )

    memory_marker = paths.memory_dir(pool) / "session" / session_id
    assert result.errors == [f"could not remove {trace_dir}: locked trace"]
    assert memory_marker.exists()

    monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    retry = asyncio.run(
        DefaultSessionArtifactCleaner(paths=paths).clean_session_artifacts(
            session_id,
            RecordScope(session_id=session_id, pool=pool),
        )
    )
    assert retry.errors == []
    assert not memory_marker.exists()


def test_cleaner_discovers_orphan_scopes_from_files_and_database(
    tmp_path: Path,
) -> None:
    class _DatabaseCleaner(SessionDatabaseCleaner):
        async def delete_session_rows(self, scope: RecordScope) -> int:
            return 0

        async def list_session_scopes(
            self,
            session_ids: frozenset[str] | None = None,
        ) -> list[RecordScope]:
            assert session_ids is None
            return [
                RecordScope(
                    session_id="db-only.coding",
                    workspace_id="workspace-a",
                    user_id="user-a",
                ),
                RecordScope(
                    session_id="duplicate.coding",
                    pool="coding",
                    workspace_id="workspace-a",
                ),
                RecordScope(
                    session_id="live.coding",
                    pool="coding",
                    workspace_id="workspace-a",
                ),
            ]

    paths = _paths_for(tmp_path)
    memory_orphan = paths.memory_dir("coding") / "session" / "memory-only.coding"
    memory_orphan.mkdir(parents=True)
    duplicate = paths.memory_dir("coding") / "session" / "duplicate.coding"
    duplicate.mkdir(parents=True)
    transcript = paths.sessions_dir / "research" / "transcript-only.research.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    live = paths.sessions_dir / "coding" / "live.coding.jsonl"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text("{}\n", encoding="utf-8")

    scopes = asyncio.run(
        DefaultSessionArtifactCleaner(
            paths=paths,
            database_cleaner=_DatabaseCleaner(),
        ).discover_orphan_scopes(
            live_session_ids=frozenset({"live.coding"}),
            workspace_id="workspace-a",
        )
    )

    assert scopes == sorted(
        [
            RecordScope(
                session_id="db-only.coding",
                workspace_id="workspace-a",
                user_id="user-a",
            ),
            RecordScope(
                session_id="duplicate.coding",
                pool="coding",
                workspace_id="workspace-a",
            ),
            RecordScope(
                session_id="memory-only.coding",
                pool="coding",
                workspace_id="workspace-a",
            ),
            RecordScope(
                session_id="transcript-only.research",
                pool="research",
                workspace_id="workspace-a",
            ),
        ],
        key=RecordScope.canonical,
    )


def test_cleaner_keeps_poolless_database_orphan_scope_unchanged(
    tmp_path: Path,
) -> None:
    poolless = RecordScope(
        session_id="db-only.coding",
        workspace_id="workspace-a",
        channel="web",
    )

    class _DatabaseCleaner(SessionDatabaseCleaner):
        async def delete_session_rows(self, scope: RecordScope) -> int:
            return 0

        async def list_session_scopes(
            self,
            session_ids: frozenset[str] | None = None,
        ) -> list[RecordScope]:
            assert session_ids is None
            return [poolless]

    scopes = asyncio.run(
        DefaultSessionArtifactCleaner(
            paths=_paths_for(tmp_path),
            database_cleaner=_DatabaseCleaner(),
        ).discover_orphan_scopes(
            live_session_ids=frozenset(),
            workspace_id="workspace-a",
        )
    )

    assert scopes == [poolless]
