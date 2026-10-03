"""Workspace boundary enforcement for file paths.

Provides a policy object that resolves paths relative to a workspace
root and verifies they remain within allowed boundaries.  Application-
level guard only — not a replacement for an OS-level sandbox.

Resolution and containment delegate to the canonical boundary seam
(:mod:`modex_agent.workspace.boundary`): expanduser → anchor relative to
root → ``resolve(strict=False)``; segment-aware ``is_relative_to``
containment, drive/case-aware on Windows, so symlink escapes resolve to
their real targets and prefix siblings never match.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modex_agent.workspace.boundary import (
    PathCanonicalizationError,
    canonicalize_path,
)

from .exceptions import WorkspaceBoundaryError


@dataclass(frozen=True)
class WorkspacePolicyConfig:
    """Configuration for workspace boundary enforcement."""

    root: str  # Workspace root directory
    allow_paths: tuple[str, ...] = ()  # Extra containment roots; no read/write distinction here
    writable_paths: tuple[str, ...] = ()  # Declared write roots; not checked by this class
    enforce: bool = True  # False skips containment, not path canonicalization


class WorkspacePolicy:
    """Enforce workspace boundary for file paths.

    A workspace root is established on construction.  All path checks
    verify that the resolved path falls within the root *or* one of the
    explicitly ``allow_paths`` entries.

    ``config.enforce=False`` skips containment checks, but ``resolve_path``
    still canonicalizes and can fail. This class does not enforce write policy;
    SecurityDecisionService owns file-tool read/write judgments and rebuilds
    this projection when evaluating the provider's current workspace root.
    """

    def __init__(self, config: WorkspacePolicyConfig) -> None:
        self._config = config
        self._root = canonicalize_path(config.root)
        # Pre-resolve allowed paths for faster checks.
        self._allowed: tuple[Path, ...] = tuple(
            canonicalize_path(p) for p in config.allow_paths
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

        Returns False for PathCanonicalizationError or an escaped path.
        """
        if not self._config.enforce:
            return True

        try:
            resolved = self._resolve_unchecked(str(path))
        except PathCanonicalizationError:
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
        return canonicalize_path(path, base=self._root)

    def _is_allowed(self, resolved: Path) -> bool:
        """Check if *resolved* is within root or any allowed path."""
        return resolved.is_relative_to(self._root) or any(
            resolved.is_relative_to(allowed) for allowed in self._allowed
        )
