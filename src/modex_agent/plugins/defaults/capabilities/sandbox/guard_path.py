"""Command-string path boundary guard.

Extracts absolute paths from command strings and checks they stay within
the configured workspace root. Resolution and containment delegate to
the canonical boundary seam
(:mod:`modex_agent.workspace.boundary`) — drive/case-aware on Windows,
symlink-resolving, segment-aware ``is_relative_to``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from modex_agent.workspace.boundary import (
    PathCanonicalizationError,
    canonicalize_path,
)

from .guard import CommandGuard, CommandSeverity, GuardMatch, GuardResult
from .guard_device import is_benign_device_path

# Regex patterns for extracting absolute paths from command strings.
# Literal absolute-path extraction; dynamic shell expressions are not contained.
_WIN_PATH_RE = re.compile(
    r"(?<![A-Za-z])(?:[A-Za-z]:[^\s\"'|><;]*|\\\\[^\s\"'|><;]+(?:\\[^\s\"'|><;]+)*)"
)
_POSIX_PATH_RE = re.compile(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)")
_HOME_PATH_RE = re.compile(r"(?:^|[\s>'\"])(~[^\s\"'>;|<]*)")


@dataclass
class PathBoundaryConfig:
    """Configuration for PathBoundaryGuard."""

    workspace_root: str | None = None
    """Workspace root directory. If None, no boundary checking is performed."""
    allow_paths: tuple[str, ...] = ()
    """Additional paths allowed outside workspace_root."""
    enabled: bool = True


class PathBoundaryGuard(CommandGuard):
    """Extract absolute paths from command strings and enforce workspace boundaries.

    Example::

        guard = PathBoundaryGuard(PathBoundaryConfig(workspace_root="/workspace"))
        result = guard.check("cat /etc/passwd")
        # result.allowed == False
    """

    def __init__(self, config: PathBoundaryConfig | None = None) -> None:
        self._config = config or PathBoundaryConfig()

    def check(self, command: str) -> GuardResult:
        """Check *command* for paths outside the configured workspace.

        Returns a :class:`GuardResult` indicating whether all extracted
        absolute paths fall within the workspace root (and any extra
        allowed paths).
        """
        if not self._config.enabled or self._config.workspace_root is None:
            return GuardResult(allowed=True)

        root = canonicalize_path(self._config.workspace_root)
        allow_roots = [
            canonicalize_path(p, base=root) for p in self._config.allow_paths
        ]

        for raw in self._extract_paths(command):
            # Expand environment variables and home
            expanded = os.path.expandvars(raw.strip())

            # Skip benign device paths
            if is_benign_device_path(expanded):
                continue

            try:
                p = canonicalize_path(expanded, base=root)
            except PathCanonicalizationError as exc:
                # An extracted absolute path that cannot be canonicalized
                # (foreign-platform form, embedded NUL) is unjudgeable —
                # fail CLOSED with the typed fact, never skip it.
                match = GuardMatch(
                    pattern="<path-boundary>",
                    severity=CommandSeverity.CRITICAL,
                    category="path_boundary",
                    description=f"Unresolvable path: {raw} ({exc})",
                )
                return GuardResult(
                    allowed=False,
                    matches=(match,),
                    reason=(
                        f"Command denied: [critical] Path outside workspace: "
                        f"{raw} (path_boundary)"
                    ),
                )

            if is_benign_device_path(str(p)):
                continue

            # Check if path is within workspace or allowed paths
            if not self._is_within_any(p, [root] + allow_roots):
                match = GuardMatch(
                    pattern="<path-boundary>",
                    severity=CommandSeverity.CRITICAL,
                    category="path_boundary",
                    description=f"Path outside workspace: {raw}",
                )
                return GuardResult(
                    allowed=False,
                    matches=(match,),
                    reason=f"Command denied: [critical] Path outside workspace: {raw} (path_boundary)",
                )

        return GuardResult(allowed=True)

    @staticmethod
    def _extract_paths(command: str) -> list[str]:
        """Extract absolute and home-relative paths from a command string.

        Returns a list of raw path strings (may contain unexpanded env vars).
        """
        win_paths = _WIN_PATH_RE.findall(command)
        posix_paths = _POSIX_PATH_RE.findall(command)
        home_paths = _HOME_PATH_RE.findall(command)
        return win_paths + posix_paths + home_paths

    @staticmethod
    def _is_within_any(path: Path, roots: list[Path]) -> bool:
        """Check if *path* is within any of the *roots* (segment-aware)."""
        return any(path.is_relative_to(root) for root in roots)
