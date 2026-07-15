"""Filesystem discovery of persisted session scope identities."""

from __future__ import annotations

from pathlib import Path

from modex_agent.core.scope import RecordScope
from modex_agent.workspace.paths import SUBDIR_MEMORY, WorkspacePaths

_SESSION_MEMORY_SUBDIR = "session"
_TRANSCRIPT_SUFFIX = ".jsonl"


def discover_file_session_scopes(
    paths: WorkspacePaths,
    workspace_id: str,
) -> list[RecordScope]:
    """Return session scopes represented by memory directories or transcripts."""
    scopes: dict[str, RecordScope] = {}
    _collect_memory_scopes(paths.root / SUBDIR_MEMORY, workspace_id, scopes)
    _collect_transcript_scopes(paths.sessions_dir, workspace_id, scopes)
    return sorted(scopes.values(), key=RecordScope.canonical)


def _collect_memory_scopes(
    memory_root: Path,
    workspace_id: str,
    scopes: dict[str, RecordScope],
) -> None:
    for pool_dir in _directories(memory_root):
        for session_dir in _directories(pool_dir / _SESSION_MEMORY_SUBDIR):
            scope = RecordScope(
                pool=pool_dir.name,
                workspace_id=workspace_id,
                session_id=session_dir.name,
            )
            scopes[scope.canonical()] = scope


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
                pool=pool_dir.name,
                workspace_id=workspace_id,
                session_id=transcript.name.removesuffix(_TRANSCRIPT_SUFFIX),
            )
            scopes[scope.canonical()] = scope


def _directories(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


__all__ = ["discover_file_session_scopes"]
