"""Shared workspace-request resolution for WebUI and control routes.

Extracts the workspace-root resolution previously embedded in
``WebUIServer._ws_root_of`` so both WebUI routes and the new control routes
share one resolution path. The resolver is a pure function over its inputs —
it does not depend on ``WebUIServer``.

Resolution rules (mirror the prior WebUI behavior exactly):
- Empty ``ws_raw`` selects home (``home_root``).
- Relative ``ws_raw`` resolves against ``relative_base`` when provided, else
  falls back to ``Path.resolve`` (CWD-based — preserves the prior degenerate
  behavior when no workspace control is wired).
- Absolute ``ws_raw`` is used directly (after ``expanduser``).
- On resolution error (``OSError`` / ``ValueError``) the result falls back to
  ``home_root`` with ``is_home=False`` (home used as fallback, not selected).

The returned :class:`WorkspaceResolution` is a frozen value object carrying
the resolved root, whether home was explicitly selected, and the raw input for
diagnostics. Derivation helpers (``sessions_dir`` / ``session_index_dir``)
build per-workspace sub-paths from the resolved root so callers do not
re-implement the ``<root>/<data_dir>/<subdir>`` layout.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from modex_agent.workspace.paths import WorkspacePaths

logger = logging.getLogger(__name__)


class WorkspaceResolution(BaseModel):
    """Structured result of resolving a ``ws`` request parameter.

    Frozen (``extra="forbid"``) per ``rules/type-safety.md`` rule 12. Carries
    the resolved workspace root plus enough context for callers to distinguish
    resolution outcomes: WebUI routes use ``root`` with their existing
    fallback behavior; control routes can reject ``is_home`` or non-absolute
    roots for stricter validation.

    Attributes:
        root: Resolved workspace root directory (absolute when resolution
            succeeded; ``home_root`` on error fallback).
        is_home: ``True`` only when home was explicitly selected (empty
            ``ws_raw``). ``False`` for explicit non-home requests and for
            error fallbacks that landed on home.
        raw_ws: The original ``ws`` input, or ``None`` when empty/omitted.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    root: Path
    is_home: bool
    raw_ws: str | None

    def sessions_dir(self, data_dir_name: str) -> Path:
        """Derive the transcript sessions dir from the resolved root.

        ``<root>/<data_dir_name>/sessions`` — the same layout
        :meth:`WebUIServer._sessions_dir_of_ws` uses for non-home workspaces.
        """
        return WorkspacePaths(root=self.root / data_dir_name).sessions_dir

    def session_index_dir(self, data_dir_name: str) -> Path:
        """Derive the session-index dir from the resolved root.

        ``<root>/<data_dir_name>/session_index`` — mirrors
        :meth:`WebUIServer._index_dir_of_ws` for non-home workspaces.
        """
        return WorkspacePaths(root=self.root / data_dir_name).session_index_dir


def resolve_ws_request(
    ws_raw: str,
    *,
    home_root: Path,
    relative_base: Path | None = None,
) -> WorkspaceResolution:
    """Resolve a ``ws`` request parameter to a workspace root.

    Single source of truth for workspace-root resolution, shared by WebUI
    routes and control routes.

    Args:
        ws_raw: The raw ``ws`` query/payload value. Empty string selects home.
        home_root: The home workspace root (canonical home). Used for empty
            selection and as the error fallback.
        relative_base: Base directory against which relative ``ws_raw`` paths
            resolve. When ``None``, relative paths are NOT re-based and
            ``Path.resolve`` resolves them against the process CWD (preserves
            the prior WebUI behavior when no workspace control is wired).

    Returns:
        A :class:`WorkspaceResolution` with the resolved root, ``is_home``
        flag, and raw input.
    """
    raw = ws_raw if ws_raw else None
    if not ws_raw:
        return WorkspaceResolution(root=home_root, is_home=True, raw_ws=raw)
    base = Path(ws_raw).expanduser()
    if not base.is_absolute() and relative_base is not None:
        base = relative_base / base
    try:
        resolved = base.resolve(strict=False)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to resolve workspace path %r: %s", ws_raw, exc)
        return WorkspaceResolution(root=home_root, is_home=False, raw_ws=raw)
    return WorkspaceResolution(root=resolved, is_home=False, raw_ws=raw)
