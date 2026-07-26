"""Shared ripgrep subprocess backend for file discovery tools.

Encapsulates the mechanical parts of invoking ``rg --files``: binary
resolution via PATH, subprocess execution, stdout line parsing, and
error normalisation. Contains NO fallback logic — callers (``GlobTool``,
``SearchFilesTool``) own their own fallback chains and decide whether
to call this backend at all.

Design reference: opencode ``packages/core/src/ripgrep.ts`` (the
``glob`` method), adapted to Python's async subprocess model.

Key flags (matching opencode):
  - ``--no-config``   — ignore user ``~/.ripgreprc`` for deterministic behaviour
  - ``--files``       — list files only (never directories)
  - ``--hidden``      — include dotfiles
  - ``--glob=!**/.git/**`` — always exclude .git directory
  - ``--glob=<pattern>``   — the caller's positive glob pattern
  - ``.``             — search path relative to pinned cwd (the search root)
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from typing import Any

from pydantic import BaseModel

__all__ = ["RipgrepResult", "RipgrepBackend"]


class RipgrepResult(BaseModel):
    """Raw result of a ``rg --files`` invocation.

    ``lines`` are relative paths cleaned of leading ``./`` and with
    backslashes normalised to forward slashes (matching opencode's
    path normalisation). ``error`` is non-None when ripgrep failed
    to execute or returned a non-{0,1} exit code.
    """

    model_config = {"frozen": True}

    lines: list[str]
    truncated: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Path normalisation — mirrors opencode ripgrep.ts parse() logic
# ---------------------------------------------------------------------------

# Strip leading ./ or .\ (repeated, e.g. ././src → src)
_LEADING_DOT_SLASH_RE = re.compile(r"^(?:\.[\\/])+")
# Strip any remaining leading path separators
_LEADING_SLASH_RE = re.compile(r"^[\\/]+")


def _normalize_rg_path(line: str) -> str:
    """Normalise one ripgrep output line to a clean relative path.

    rg prints paths relative to its cwd (the search root), prefixed
    with ``./`` and using native separators. We strip the prefix and
    normalise to forward slashes for cross-platform consistency.
    """
    cleaned = _LEADING_DOT_SLASH_RE.sub("", line)
    cleaned = _LEADING_SLASH_RE.sub("", cleaned)
    return cleaned.replace("\\", "/")


# ---------------------------------------------------------------------------
# Async subprocess helper (matches search_tool.py pattern)
# ---------------------------------------------------------------------------


async def _async_subprocess_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return await asyncio.to_thread(subprocess.run, *args, **kwargs)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class RipgrepBackend:
    """Shared ripgrep invocation backend.

    Resolves ripgrep via ``shutil.which`` (PATH lookup only — no binary
    bundling or download). Stateless; all methods are static.

    Usage::

        if RipgrepBackend.available():
            result = await RipgrepBackend.list_files(
                cwd=str(search_root), pattern="**/*.py", limit=100,
            )
            if result.error is None:
                # use result.lines
    """

    @staticmethod
    def available() -> bool:
        """True if ripgrep is on PATH."""
        return shutil.which("rg") is not None

    @staticmethod
    def resolve() -> str | None:
        """Return the absolute path to ripgrep, or None if not found."""
        return shutil.which("rg")

    @staticmethod
    async def list_files(
        *,
        cwd: str,
        pattern: str,
        limit: int = 100,
        hidden: bool = True,
        exclude_git: bool = True,
        timeout: int = 30,
    ) -> RipgrepResult:
        """Run ``rg --files`` with a glob pattern.

        Args:
            cwd: Directory to search in. The rg process cwd is pinned
                here so ``--glob`` patterns match relative to this root.
            pattern: Glob pattern (e.g. ``"**/*.py"``, ``"*.{ts,tsx}"``).
                Brace expansion is handled by ripgrep's glob engine.
            limit: Maximum results. We read all output then slice;
                truncation is detected when more than ``limit`` lines
                are returned.
            hidden: Include hidden files (dotfiles) via ``--hidden``.
            exclude_git: Exclude ``.git`` directory via
                ``--glob=!**/.git/**``.
            timeout: Subprocess timeout in seconds.

        Returns:
            RipgrepResult with cleaned relative paths. ``error`` is
            non-None if ripgrep is missing, times out, or exits with
            an unexpected code.
        """
        rg_path = RipgrepBackend.resolve()
        if rg_path is None:
            return RipgrepResult(
                lines=[], truncated=False, error="ripgrep not found on PATH"
            )

        args: list[str] = [rg_path, "--no-config", "--files"]
        if hidden:
            args.append("--hidden")
        # Positive pattern FIRST, then exclusions — ripgrep's "last matching
        # glob wins" rule means a positive glob after a negative one re-includes
        # the file.  Matching opencode's ordering keeps the exclusion effective.
        # When the pattern is "*" (match-all), skip the positive glob entirely
        # so that .gitignore is still respected (any --glob overrides ignore
        # logic per ripgrep docs).
        if pattern != "*":
            args.extend(["--glob", pattern])
        if exclude_git:
            args.extend(["--glob", "!**/.git/**"])
        args.append(".")

        try:
            proc = await _async_subprocess_run(
                args, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return RipgrepResult(
                lines=[], truncated=False, error=f"ripgrep timed out after {timeout}s"
            )
        except Exception as e:
            return RipgrepResult(
                lines=[], truncated=False, error=f"ripgrep execution failed: {e}"
            )

        # rg exit codes: 0 = matches found, 1 = no matches, 2+ = error
        if proc.returncode not in (0, 1):
            stderr = proc.stderr.strip()[:500]
            return RipgrepResult(
                lines=[],
                truncated=False,
                error=f"ripgrep failed (exit {proc.returncode}): {stderr}",
            )

        # Parse and normalise output lines
        all_lines: list[str] = []
        for raw_line in proc.stdout.splitlines():
            stripped = raw_line.strip()
            if stripped:
                all_lines.append(_normalize_rg_path(stripped))

        truncated = len(all_lines) > limit
        if truncated:
            all_lines = all_lines[:limit]

        return RipgrepResult(lines=all_lines, truncated=truncated, error=None)
