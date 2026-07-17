"""Path primitives layer for the workspace manager.

Converges the two prior sanitizers — ``framework/core/session_store.py::
safe_filename`` and ``framework/runtime/store.py::
JsonFileTurnStateStore._safe_segment`` — into a single ``safe_segment`` plus a
frozen :class:`WorkspacePaths` value object that guarantees no accessor can
escape its root.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Layout constants — single source of truth for the on-disk workspace layout.
# ---------------------------------------------------------------------------

SUBDIR_MEMORY: str = "memory"
SUBDIR_MEDIA: str = "media"
SUBDIR_RUNTIME: str = "runtime_state"
SUBDIR_INBOX: str = "inbox"
SUBDIR_EXPERIENCES: str = "experiences"
SUBDIR_POOL_SESSIONS: str = "pool_sessions"
SUBDIR_SESSIONS: str = "sessions"
SUBDIR_SESSION_INDEX: str = "session_index"
SUBDIR_OVERFLOW: str = "overflow"
SUBDIR_TODOS: str = "todos"
SUBDIR_TURNS: str = "turns"
SUBDIR_COMMANDS: str = "commands"
SUBDIR_TRACE: str = "trace"
SUBDIR_OUTPUT: str = "output"
SUBDIR_PRUNED: str = "pruned"
WORKSPACE_STATE_DB: str = "state.db"
# Reserved global-tier directory name (workspace registry + conversation map),
# NOT a per-workspace subdir. WorkspacePaths accessors must never produce it.
RESERVED_GLOBAL_DIR: str = "_registry"

# Leaves permitted under the per-pool runtime directory.
_RUNTIME_LEAVES: frozenset[str] = frozenset(
    {SUBDIR_TURNS, SUBDIR_COMMANDS, SUBDIR_TRACE, SUBDIR_OUTPUT, SUBDIR_TODOS}
)

# Anything outside [A-Za-z0-9_-] is neutralized to ``_``. Dots are excluded
# from the allowed set to converge with ``JsonFileTurnStateStore._SAFE_RE``
# (the stricter of the two source implementations named in the module docstring).
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def safe_segment(name: str) -> str:
    """Sanitize a single path segment so it cannot escape a root.

    Replaces every character outside ``[A-Za-z0-9_-]`` with ``_``, strips
    whitespace, removes any residual ``..`` (already neutered by the regex,
    but belt-and-braces), and returns ``"_"`` for empty/whitespace-only input.
    """
    # Strip whitespace first so whitespace-only input collapses to empty.
    stripped = name.strip()
    sanitized = _UNSAFE_CHARS.sub("_", stripped)
    sanitized = sanitized.replace("..", "")
    return sanitized or "_"


def is_reserved_segment(segment: str) -> bool:
    """True for the global-tier reserved directory name (``_registry``).

    WorkspacePaths accessors sanitize arbitrary segments via
    :func:`safe_segment`; this helper identifies the one name reserved for
    global-tier state (registry + conversation map) so it can never collide
    with a per-workspace subdir.
    """
    return segment == RESERVED_GLOBAL_DIR


@dataclass(frozen=True)
class WorkspacePaths:
    """Frozen value object exposing typed, escape-proof accessors.

    Every accessor routes through :meth:`_child`, which sanitizes each segment
    with :func:`safe_segment`, joins it under :attr:`root`, resolves, and
    raises :class:`ValueError` if the result is not contained by ``root``.
    """

    root: Path

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks normal assignment; use object.__setattr__.
        object.__setattr__(self, "root", self.root.resolve())

    # ------------------------------------------------------------------
    # Internal builder
    # ------------------------------------------------------------------

    def _child(self, *parts: str) -> Path:
        sanitized = [safe_segment(p) for p in parts]
        candidate = self.root.joinpath(*sanitized).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError(
                f"Resolved path {candidate} escapes workspace root {self.root}"
            )
        return candidate

    # ------------------------------------------------------------------
    # Per-pool accessors
    # ------------------------------------------------------------------

    def memory_dir(self, pool: str) -> Path:
        return self._child(SUBDIR_MEMORY, pool)

    def media_dir(self, pool: str) -> Path:
        return self._child(SUBDIR_MEDIA, pool)

    def pruned_dir(self, pool: str) -> Path:
        return self._child(SUBDIR_MEMORY, pool, SUBDIR_PRUNED)

    def runtime_dir(self, pool: str, leaf: str) -> Path:
        if leaf not in _RUNTIME_LEAVES:
            raise ValueError(
                f"Invalid runtime leaf {leaf!r}; expected one of "
                f"{sorted(_RUNTIME_LEAVES)}"
            )
        return self._child(SUBDIR_RUNTIME, pool, leaf)

    def experience_dir(self, pool: str, agent: str) -> Path:
        return self._child(SUBDIR_EXPERIENCES, pool, agent)

    # ------------------------------------------------------------------
    # Workspace-level accessors (properties)
    # ------------------------------------------------------------------

    @property
    def inbox_dir(self) -> Path:
        return self._child(SUBDIR_INBOX)

    @property
    def state_db(self) -> Path:
        return self.root / WORKSPACE_STATE_DB

    @property
    def pool_sessions_dir(self) -> Path:
        return self._child(SUBDIR_POOL_SESSIONS)

    @property
    def sessions_dir(self) -> Path:
        return self._child(SUBDIR_SESSIONS)

    @property
    def session_index_dir(self) -> Path:
        return self._child(SUBDIR_SESSION_INDEX)

    @property
    def overflow_dir(self) -> Path:
        return self._child(SUBDIR_OVERFLOW)

    # ------------------------------------------------------------------
    # Skeleton
    # ------------------------------------------------------------------

    def mkdir_skeleton(self) -> None:
        """Create the five workspace-level directories."""
        for d in (
            self.inbox_dir,
            self.pool_sessions_dir,
            self.sessions_dir,
            self.session_index_dir,
            self.overflow_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
