"""Tool argument matching for path-based tool approval classification.

ArgumentMatcher is used by ApprovalRuntime.classifier to check whether
tool path arguments fall within allowed directories. It is NOT an approval
interceptor — it is a pure classification helper."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


def _looks_like_path(val: str) -> bool:
    """Heuristic: does the value look like a filesystem path?"""
    if "/" in val or "\\" in val:
        return True
    if len(val) >= 2 and val[1] == ":" and val[0].isalpha():
        return True
    return val.endswith((".txt", ".py", ".json", ".yml", ".yaml", ".md", ".csv"))


class ArgumentMatcher:
    """Match tool path arguments against allowed directory roots.

    A tool call is allowed when every path argument resolves to a real
    absolute path contained by at least one ``allowed_paths`` root. Paths
    are fully resolved (``expanduser`` -> anchor to ``project_root`` ->
    ``Path.resolve``) so ``..`` segments collapse and cannot escape, and
    containment is segment-aware (``is_relative_to``) — never raw string
    prefix or glob matching, which would let ``*`` cross ``/`` and admit
    ``../`` escapes.

    ``allowed_paths`` entries are directory roots. A trailing ``/*`` or
    ``/**`` means "this directory, recursively"; a bare ``*`` or ``**``
    allows everywhere. Example: ``["./*"]`` is the whole project tree.

    The base that relative paths (and relative ``allowed_paths`` like ``./*``)
    anchor to is, in order of precedence: ``root_provider.current()`` (the live
    active-workspace working dir, read on every call so a workspace switch needs
    no re-wiring — the SAME provider the file tools use), then ``project_root``,
    then the process cwd. Reusing ``WorkspaceRootProvider`` keeps approval path
    resolution converged with workspace switching instead of pinning a static
    root at construction time.
    """

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        root_provider: WorkspaceRootProvider | None = None,
    ) -> None:
        self.project_root = project_root
        self.root_provider = root_provider

    def _base(self) -> Path:
        """The directory relative paths anchor to (live workspace > static > cwd)."""
        if self.root_provider is not None:
            return self.root_provider.current()
        if self.project_root is not None:
            return self.project_root
        return Path.cwd()

    def matches(self, arguments: dict[str, Any], allowed_paths: list[str]) -> bool:
        """True if all path arguments resolve inside at least one allowed root."""
        paths = self._extract_paths(arguments)
        if not paths:
            return True
        base = self._base()  # hoisted — invariant for the duration of this call
        return all(self._match_any(self._resolve_path(raw, base), allowed_paths) for raw in paths)

    def _resolve_path(self, raw: str, base: Path | None = None) -> Path:
        """Resolve *raw* to a real absolute path.

        ``~`` expands to the user home; relative paths anchor to *base* (or
        ``_base()`` when not supplied — see class docstring for precedence); the
        result is fully resolved so ``..`` segments collapse and symlinks
        resolve.
        """
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (base if base is not None else self._base()) / p
        return p.resolve(strict=False)

    def _match_any(self, path: Path, patterns: list[str]) -> bool:
        """True if *path* is contained by at least one allowed root pattern.

        ``path`` is normalized here (cheap, once per call) because callers like
        the public ``matches`` pass through ``_resolve_path`` but some callers
        (tests, ``_allowed_root`` consumers) pass raw ``Path`` instances that
        may not yet be fully resolved. Pattern resolution is delegated to
        ``_allowed_root`` so we don't redo it here.
        """
        resolved = path.resolve(strict=False)
        return any(self._matches_pattern(resolved, pattern) for pattern in patterns)

    def _matches_pattern(self, path: Path, pattern: str) -> bool:
        stripped = pattern.strip()
        if stripped in ("*", "**"):
            return True
        if stripped == "":
            return False
        return path.is_relative_to(self._allowed_root(stripped))

    def _allowed_root(self, pattern: str) -> Path:
        """Resolve an allowed_paths pattern to its directory root.

        Strips a trailing ``/**``, ``/*``, or ``*`` glob marker so the
        remaining directory anchors the segment-aware containment check.
        """
        p = pattern
        if p.endswith("/**"):
            p = p[:-3]
        elif p.endswith("/*"):
            p = p[:-2]
        elif p.endswith("*"):
            p = p[:-1]
        p = p.rstrip("/") or "."
        return self._resolve_path(p)

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
