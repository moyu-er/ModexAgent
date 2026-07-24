"""Glob tool — file pattern matching with rg → fd → Python fallback.

Finds files matching a glob pattern. Powered by ripgrep (``rg --files``)
when available, falls back to ``fd``, then to Python ``pathlib.rglob``.
Respects ``.gitignore`` by default (ripgrep/fd native; Python fallback
parses root ``.gitignore``). Dotfiles are included; the ``.git``
directory is always excluded.

Brace expansion (e.g. ``*.{ts,tsx}``) is supported by all backends.
The Python fallback expands braces manually via ``_fallback.expand_braces``.

Design reference: opencode ``packages/core/src/tool/glob.ts`` +
the ``rg --files`` invocation in ``packages/core/src/ripgrep.ts``.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...core.tool_manager import Tool
from ._fallback import DEFAULT_EXCLUDES, expand_braces, is_ignored, load_gitignore
from ._ripgrep import RipgrepBackend

__all__ = ["GlobTool"]

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 200


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


async def _async_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return await asyncio.to_thread(subprocess.run, *args, **kwargs)


def _to_forward_slashes(path: str) -> str:
    return path.replace("\\", "/")


class GlobTool(Tool):
    """Find files by glob pattern.

    Backend chain (first available wins):

    1. **ripgrep** (``rg --no-config --files --hidden --glob=<pattern>
       --glob=!**/.git/** .``) — native brace expansion, gitignore
       support, .git exclusion.
    2. **fd** (``fd --type f --hidden --glob --exclude .git
       --max-results <limit> <pattern> <path>``) — native brace
       expansion, gitignore support.
    3. **Python pathlib** (``Path.rglob(expand_braces(pattern))``) —
       manual brace expansion, root ``.gitignore`` parsing, hardcoded
       exclusion of common heavy directories.

    Results are returned as relative paths (relative to the search
    root), one per line, capped at ``limit``.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return (
            "- Fast file pattern matching tool that works with any codebase size\n"
            '- Supports glob patterns like "**/*.js" or "src/**/*.ts"\n'
            "- Returns matching file paths (files only, never directories)\n"
            "- Respects .gitignore by default; dotfiles are included\n"
            "- Use this tool when you need to find files by name patterns\n"
            "- Good patterns:\n"
            "  - *.ts — all files matching an extension, at any depth\n"
            "  - src/*.ts — files directly inside src/ (one level, not recursive)\n"
            "  - src/**/*.ts — recursive walk with a subdirectory anchor\n"
            "  - **/*.py — recursive walk from the search root\n"
            "  - *.{ts,tsx} — brace expansion is supported\n"
            "  - {src,test}/**/*.ts — cartesian brace expansion too\n"
            "- Results are capped at 100 matches; use a more specific pattern to narrow\n"
            "- Avoid recursing into node_modules/, .venv/, __pycache__/ — "
            "prefer specific subpaths\n"
            "- When doing an open-ended search that may need multiple rounds of "
            "globbing and grepping, consider delegating to a subagent instead\n"
            "- You can call multiple tools in a single response — batch speculative "
            "searches that are potentially useful"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "The directory to search in. "
                        "Defaults to the current working directory."
                    ),
                    "default": ".",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Maximum results to return (default: {_DEFAULT_LIMIT}, "
                        f"max: {_MAX_LIMIT})"
                    ),
                    "default": _DEFAULT_LIMIT,
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        limit: int = _DEFAULT_LIMIT,
        **kwargs: Any,
    ) -> str:
        if isinstance(limit, str):
            limit = int(limit)

        search_path = _resolve_path(path)
        if not search_path.exists():
            return f"Error: Path not found: {path}"
        if not search_path.is_dir():
            return f"Error: glob path must be a directory: {path}"

        limit = min(max(limit, 1), _MAX_LIMIT)

        if RipgrepBackend.available():
            result = await RipgrepBackend.list_files(
                cwd=str(search_path),
                pattern=pattern,
                limit=limit,
                hidden=True,
                exclude_git=True,
            )
            if result.error is None:
                return self._format_result(result.lines, result.truncated, limit)

        if shutil.which("fd") is not None:
            fd_result = await self._glob_with_fd(pattern, search_path, limit)
            if not fd_result.startswith("Error:"):
                return fd_result

        return await self._glob_with_python(pattern, search_path, limit)

    async def _glob_with_fd(
        self,
        pattern: str,
        search_path: Path,
        limit: int,
    ) -> str:
        cmd = [
            "fd",
            "--type", "f",
            "--hidden",
            "--glob",
            "--exclude", ".git",
            "--max-results", str(limit),
            pattern,
            str(search_path),
        ]
        try:
            proc = await _async_subprocess_run(
                cmd, capture_output=True, text=True, timeout=30
            )
            if proc.returncode != 0:
                return f"Error: fd failed (exit {proc.returncode}): {proc.stderr[:200]}"
            files: list[str] = []
            for ln in proc.stdout.strip().splitlines():
                if not ln.strip():
                    continue
                try:
                    rel = Path(ln).relative_to(search_path)
                    files.append(_to_forward_slashes(str(rel)))
                except ValueError:
                    files.append(_to_forward_slashes(ln))
            truncated = len(files) >= limit
            return self._format_result(files, truncated, limit)
        except subprocess.TimeoutExpired:
            return "Error: fd search timed out after 30 seconds"
        except Exception as e:
            return f"Error: fd execution failed: {e}"

    async def _glob_with_python(
        self,
        pattern: str,
        search_path: Path,
        limit: int,
    ) -> str:
        patterns = expand_braces(pattern)
        positive_gi, negative_gi = load_gitignore(search_path)

        files: list[str] = []
        seen: set[str] = set()

        for p in patterns:
            p = p.lstrip("/")
            for file_path in search_path.rglob(p):
                if not file_path.is_file():
                    continue
                if DEFAULT_EXCLUDES & set(file_path.parts):
                    continue
                try:
                    rel = file_path.relative_to(search_path)
                except ValueError:
                    rel = file_path
                rel_str = _to_forward_slashes(str(rel))
                if rel_str in seen:
                    continue
                if is_ignored(rel_str, rel.parts, positive_gi, negative_gi):
                    continue
                seen.add(rel_str)
                files.append(rel_str)
                if len(files) > limit:
                    break
            if len(files) > limit:
                break

        truncated = len(files) > limit
        if truncated:
            files = files[:limit]
        return self._format_result(files, truncated, limit)

    @staticmethod
    def _format_result(files: list[str], truncated: bool, limit: int) -> str:
        if not files:
            return "No files found"

        lines = list(files)
        if truncated:
            lines.append("")
            lines.append(
                f"(Results are truncated: showing first {limit} results. "
                f"Consider using a more specific path or pattern.)"
            )
        return "\n".join(lines)
