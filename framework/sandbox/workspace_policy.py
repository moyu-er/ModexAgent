"""Workspace boundary enforcement for file paths.

Provides a policy object that resolves paths relative to a workspace
root and verifies they remain within allowed boundaries.  Application-
level guard only — not a replacement for an OS-level sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .exceptions import WorkspaceBoundaryError


@dataclass(frozen=True)
class WorkspacePolicyConfig:
    """Configuration for workspace boundary enforcement."""

    root: str                                  # Workspace root directory
    allow_paths: tuple[str, ...] = ()          # Extra allowed read-only paths
    writable_paths: tuple[str, ...] = ()       # Extra allowed write paths
    enforce: bool = True                       # False = all checks pass


class WorkspacePolicy:
    """Enforce workspace boundary for file paths.

    A workspace root is established on construction.  All path checks
    verify that the resolved path falls within the root *or* one of the
    explicitly ``allow_paths`` entries.

    When ``config.enforce`` is ``False`` every check passes (useful for
    development / testing).
    """

    def __init__(self, config: WorkspacePolicyConfig) -> None:
        self._config = config
        self._root = Path(config.root).resolve()
        # Pre-resolve allowed paths for faster checks.
        self._allowed: tuple[Path, ...] = tuple(
            Path(p).resolve() for p in config.allow_paths
        )

    @property
    def root(self) -> Path:
        """Resolved absolute workspace root."""
        return self._root

    # -- public API ----------------------------------------------------------

    def resolve_path(self, path: str) -> Path:
        """Resolve *path* and verify it is within the workspace.

        Steps: expanduser -> resolve relative to root -> Path.resolve ->
        boundary check.

        Raises:
            WorkspaceBoundaryError: If the resolved path escapes the
                workspace root and all allowed paths.
        """
        if not self._config.enforce:
            return self._resolve_unchecked(path)

        resolved = self._resolve_unchecked(path)

        if not self._is_allowed(resolved):
            raise WorkspaceBoundaryError(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                f"the workspace root '{self._root}'"
            )

        return resolved

    def is_within(self, path: str | Path) -> bool:
        """Check whether *path* is within the workspace or allowed paths.

        Returns ``False`` (never raises) for any invalid or escaped path.
        """
        if not self._config.enforce:
            return True

        try:
            resolved = self._resolve_unchecked(str(path))
        except Exception:
            return False

        return self._is_allowed(resolved)

    def require_within(self, path: str | Path) -> None:
        """Assert *path* is within the workspace.

        Raises:
            WorkspaceBoundaryError: If the path is outside boundaries.
        """
        if not self._config.enforce:
            return

        if not self.is_within(path):
            raise WorkspaceBoundaryError(
                f"Path '{path}' is outside the allowed workspace '{self._root}'"
            )

    # -- internal helpers ----------------------------------------------------

    def _resolve_unchecked(self, path: str) -> Path:
        """Expand user directory and resolve relative paths against root."""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        return candidate.resolve(strict=False)

    def _is_allowed(self, resolved: Path) -> bool:
        """Check if *resolved* is within root or any allowed path."""
        # Check workspace root.
        try:
            resolved.relative_to(self._root)
            return True
        except ValueError:
            pass

        # Check explicitly allowed paths.
        for allowed in self._allowed:
            try:
                resolved.relative_to(allowed)
                return True
            except ValueError:
                continue

        return False
