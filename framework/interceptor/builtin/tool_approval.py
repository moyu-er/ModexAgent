"""Tool argument matching for path-based tool approval classification.

ArgumentMatcher is used by ApprovalRuntime.classifier to check whether
tool path arguments fall within allowed directories. It is NOT an approval
interceptor — it is a pure classification helper."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any


def _looks_like_path(val: str) -> bool:
    """Heuristic: does the value look like a filesystem path?"""
    if "/" in val or "\\" in val:
        return True
    if len(val) >= 2 and val[1] == ":" and val[0].isalpha():
        return True
    return val.endswith((".txt", ".py", ".json", ".yml", ".yaml", ".md", ".csv"))


class ArgumentMatcher:
    """Match tool arguments against allowed path patterns for approval.

    Uses fnmatch for cross-platform wildcard support (*, ?, []).
    Resolves . to project_root, ~ to user home.
    """

    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = project_root

    def matches(self, arguments: dict[str, Any], allowed_paths: list[str]) -> bool:
        """Returns True if all path arguments match at least one allowed pattern."""
        paths = self._extract_paths(arguments)
        if not paths:
            return True
        for path in paths:
            resolved = self._resolve_path(path)
            if not self._match_any(resolved, allowed_paths):
                return False
        return True

    def _resolve_path(self, raw: str) -> Path:
        if raw.startswith("~/"):
            return Path.home() / raw[2:]
        if raw == ".":
            return self.project_root if self.project_root is not None else Path(".").resolve()
        if raw.startswith("./"):
            root = self.project_root if self.project_root is not None else Path(".").resolve()
            return root / raw[2:]
        return Path(raw).expanduser()

    def _match_any(self, path: Path, patterns: list[str]) -> bool:
        path_str = str(path).replace("\\", "/")
        for pattern in patterns:
            resolved_pattern = self._resolve_path(pattern)
            pattern_str = str(resolved_pattern).replace("\\", "/")
            if fnmatch.fnmatch(path_str, pattern_str):
                return True
        return False

    def _extract_paths(self, arguments: dict[str, Any]) -> list[str]:
        path_keys = {
            "path",
            "file_path",
            "target",
            "dest",
            "directory",
            "dir",
            "working_dir",
        }
        paths: list[str] = []
        for key, value in arguments.items():
            if key in path_keys and isinstance(value, str):
                paths.append(value)
        return paths

    def is_allowed(self, tool_call) -> bool:
        """Legacy API — delegates to matches() with empty allowed_paths."""
        args = tool_call.arguments or {}
        return self.matches(args, [])
