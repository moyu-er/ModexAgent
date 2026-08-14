"""Shared helpers for Python fallback backends (no rg/fd available).

Provides:
  - ``expand_braces``: brace expansion for glob patterns (``*.{ts,tsx}`` → ``["*.ts", "*.tsx"]``)
  - ``load_gitignore``: parse root ``.gitignore`` into positive/negative pattern lists
  - ``is_ignored``: check if a relative path matches gitignore patterns
  - ``DEFAULT_EXCLUDES``: common heavy/VCS directories to always skip in Python fallback

These are ONLY used when ripgrep/fd are unavailable. The rg/fd backends
handle brace expansion and gitignore natively.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

__all__ = [
    "DEFAULT_EXCLUDES",
    "expand_braces",
    "load_gitignore",
    "is_ignored",
]

DEFAULT_EXCLUDES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        "coverage",
        ".turbo",
    }
)


def expand_braces(pattern: str) -> list[str]:
    """Expand brace expansion in a glob pattern.

    ``*.{ts,tsx}`` → ``["*.ts", "*.tsx"]``
    ``{src,test}/**/*.ts`` → ``["src/**/*.ts", "test/**/*.ts"]``
    ``*.{a,{b,c}}`` → ``["*.a", "*.b", "*.c"]`` (nested)

    Returns a list with the original pattern if no braces are present.
    """
    if "{" not in pattern:
        return [pattern]

    results = [pattern]
    while True:
        new_results: list[str] = []
        expanded = False
        for p in results:
            start = p.find("{")
            if start == -1:
                new_results.append(p)
                continue
            end = p.find("}", start)
            if end == -1:
                new_results.append(p)
                continue
            prefix = p[:start]
            suffix = p[end + 1 :]
            options = p[start + 1 : end].split(",")
            for opt in options:
                new_results.append(prefix + opt + suffix)
            expanded = True
        results = new_results
        if not expanded:
            break
    return results


def load_gitignore(root: Path) -> tuple[list[str], list[str]]:
    """Parse the root ``.gitignore`` file.

    Returns ``(positive_patterns, negative_patterns)``.
    Negative patterns (starting with ``!``) re-include files.

    Only reads the root ``.gitignore`` — nested ``.gitignore`` files
    are not traversed (simplification for fallback performance).
    """
    positive: list[str] = []
    negative: list[str] = []

    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return positive, negative

    try:
        content = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return positive, negative

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            negative.append(line[1:])
        else:
            positive.append(line)

    return positive, negative


def _match_one(rel_path: str, parts: tuple[str, ...], pattern: str) -> bool:
    """Check if a relative path matches a single gitignore pattern."""
    p = pattern.rstrip("/")
    if not p:
        return False

    root_anchored = p.startswith("/")
    if root_anchored:
        p = p[1:]

    if root_anchored:
        if "/" in p:
            if fnmatch.fnmatch(rel_path, p) or rel_path.startswith(p + "/"):
                return True
        else:
            if parts and fnmatch.fnmatch(parts[0], p):
                return True
    else:
        if "/" in p:
            if fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(rel_path, p + "/*"):
                return True
        else:
            for part in parts:
                if fnmatch.fnmatch(part, p):
                    return True
            if fnmatch.fnmatch(rel_path, p):
                return True

    return False


def is_ignored(
    rel_path: str,
    parts: tuple[str, ...],
    positive: list[str],
    negative: list[str],
) -> bool:
    """Check if a path is ignored by gitignore patterns.

    A path is ignored if it matches any positive pattern AND does not
    match any negative (re-include) pattern.
    """
    matched = False
    for pattern in positive:
        if _match_one(rel_path, parts, pattern):
            matched = True
            break

    if not matched:
        return False

    return all(not _match_one(rel_path, parts, pattern) for pattern in negative)
