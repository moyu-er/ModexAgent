"""Filesystem discovery of persisted session scope identities."""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.scope import RecordScope

# TODO(adr-0006): this import violates ADR-0006 — core runtime-imports
# workspace (tier 2). `tests/architecture/test_dependency_tree.py
# ::test_core_no_unexpected_runtime_upward_imports` fails on this file
# because of it. The violation predates the prompt-configuration feature
# branch (introduced by the SQLite persistence refactor, commit 5ef3ee7a)
# and is tracked as a known pre-existing issue. The fix is a dependency
# inversion: move the consumed surfaces (SUBDIR_MEMORY, WorkspacePaths)
# down into core, or relocate this file out of core.
from modex_agent.workspace.paths import SUBDIR_MEMORY, WorkspacePaths

_SESSION_MEMORY_SUBDIR = "session"
_TRANSCRIPT_SUFFIX = ".jsonl"


def discover_file_session_scopes(
    paths: WorkspacePaths,
    workspace_id: str,
) -> list[RecordScope]:
    """Return session scopes represented by memory directories or transcripts.

    The returned scopes are base ``RecordScope`` instances — they do not carry
    the ``pool`` dimension (ADR-0028: pool is a business-layer concept on
    ``BotRecordScope``, not on the framework base). Use
    :func:`discover_file_session_pool_map` to recover the pool directory name
    for each discovered scope when the caller needs pool-aware file cleanup.
    """
    scopes: dict[str, RecordScope] = {}
    _collect_memory_scopes(paths.root / SUBDIR_MEMORY, workspace_id, scopes)
    _collect_transcript_scopes(paths.sessions_dir, workspace_id, scopes)
    return sorted(scopes.values(), key=RecordScope.canonical)


def discover_file_session_pool_map(
    paths: WorkspacePaths,
    workspace_id: str | None = None,
) -> dict[str, str]:
    """Map each discovered session's canonical scope to its pool directory name.

    Complement to :func:`discover_file_session_scopes`: the scopes themselves
    are pool-agnostic (framework layer), but the filesystem layout is
    pool-partitioned. This map lets business-layer callers (e.g. session GC)
    recover the pool directory name for each discovered scope without the
    framework layer needing to know about ``BotRecordScope``.

    ``workspace_id`` must match the value passed to
    :func:`discover_file_session_scopes` so the canonical keys align — without
    it the pool-map keys would miss the ``workspace_id`` dimension and fail to
    match scopes that carry it.
    """
    pool_map: dict[str, str] = {}
    _collect_memory_pool_map(paths.root / SUBDIR_MEMORY, pool_map, workspace_id)
    _collect_transcript_pool_map(paths.sessions_dir, pool_map, workspace_id)
    return pool_map


def _collect_memory_scopes(
    memory_root: Path,
    workspace_id: str,
    scopes: dict[str, RecordScope],
) -> None:
    for pool_dir in _directories(memory_root):
        for session_dir in _directories(pool_dir / _SESSION_MEMORY_SUBDIR):
            scope = RecordScope(
                workspace_id=workspace_id,
                session_id=session_dir.name,
            )
            scopes[scope.canonical()] = scope


def _collect_memory_pool_map(
    memory_root: Path,
    pool_map: dict[str, str],
    workspace_id: str | None,
) -> None:
    for pool_dir in _directories(memory_root):
        for session_dir in _directories(pool_dir / _SESSION_MEMORY_SUBDIR):
            scope = RecordScope(
                workspace_id=workspace_id,
                session_id=session_dir.name,
            )
            pool_map[scope.canonical()] = pool_dir.name


def _collect_transcript_scopes(
    sessions_root: Path,
    workspace_id: str,
    scopes: dict[str, RecordScope],
) -> None:
    for pool_dir in _directories(sessions_root):
        for transcript in sorted(pool_dir.glob(f"*{_TRANSCRIPT_SUFFIX}")):
            if not transcript.is_file():
                continue
            scope = RecordScope(
                workspace_id=workspace_id,
                session_id=transcript.name.removesuffix(_TRANSCRIPT_SUFFIX),
            )
            scopes[scope.canonical()] = scope


def _collect_transcript_pool_map(
    sessions_root: Path,
    pool_map: dict[str, str],
    workspace_id: str | None,
) -> None:
    for pool_dir in _directories(sessions_root):
        for transcript in sorted(pool_dir.glob(f"*{_TRANSCRIPT_SUFFIX}")):
            if not transcript.is_file():
                continue
            scope = RecordScope(
                workspace_id=workspace_id,
                session_id=transcript.name.removesuffix(_TRANSCRIPT_SUFFIX),
            )
            pool_map[scope.canonical()] = pool_dir.name


def _directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


__all__ = ["discover_file_session_scopes", "discover_file_session_pool_map"]
