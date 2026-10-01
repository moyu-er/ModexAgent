"""Filesystem-safe name sanitization — the pure path-segment helpers.

Two sanitizers converged here (W3b) because each had consumers on both sides
of a same-level package seam:

- :func:`safe_segment` — the workspace-layout segment sanitizer (formerly
  ``workspace/paths.py``); the workspace path object, the media store, and
  the session-artifact cleaner all derive identical segments through it.
- :func:`sanitize_scope_key` / :func:`ensure_scope_dir` — the memory
  scope-key sanitizers (formerly ``memory/stores/utils.py``); memory file
  backends and the session-artifact cleaner share the on-disk scope layout.

Pure leaf: stdlib + ``pathvalidate`` only.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pathvalidate import sanitize_filename

MAX_FILENAME_LENGTH = 100

# Anything outside [A-Za-z0-9_-] is neutralized to ``_``. Dots are excluded
# from the allowed set to converge with the turn-state segment transform
# (the stricter of the two original implementations).
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


def sanitize_scope_key(scope_key: str) -> str:
    """Sanitize *scope_key* into a filesystem-safe directory name.

    Uses ``pathvalidate.sanitize_filename`` which handles platform-
    specific invalid characters (Windows reserved names, Linux ``/``,
    etc.) and replaces them with ``_``.
    """
    if not scope_key:
        return "_empty_"

    safe = sanitize_filename(scope_key, replacement_text="_")

    if len(safe) <= MAX_FILENAME_LENGTH:
        return safe or "_empty_"

    digest = hashlib.md5(scope_key.encode("utf-8")).hexdigest()[:16]
    truncated = safe[: MAX_FILENAME_LENGTH - len(digest) - 1]
    return f"{truncated}_{digest}"


def ensure_scope_dir(workspace: Path, scope_key: str) -> Path:
    """Get (creating if needed) the storage directory for a scope_key."""
    safe_key = sanitize_scope_key(scope_key)
    scope_dir = workspace / safe_key
    scope_dir.mkdir(parents=True, exist_ok=True)
    return scope_dir
