"""Bundled CLI binary resolution and private-layer PATH injection.

Runtime half of the two-layer PATH strategy for the bot installer
(the installer-only Windows-registry half lives in the installer side,
``examples/bot_project/packaging/windows/path_registry.py``):

**Private layer** (bot-only, transient):
    Bundled ``rg`` at ``<install>/bin/<platform>/``.  Injected into
    the bot process's ``os.environ["PATH"]`` at startup
    (:func:`ensure_bundled_bin_on_path`) and into every child process env
    built by :func:`modex_agent.utils.child_env.build_full_env`.
    Never written to the registry — disappears when the bot process exits.
    Uses exact-match cleanup (no marker) because process-env is transient:
    there's no reinstall-to-different-dir concern.

Both sides share :func:`prepend_path_idempotent` — the single pure
function that removes existing entries (exact or marker-matched) before
prepending the new one, guaranteeing exactly one entry regardless of how
many times the installer or startup runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "prepend_path_idempotent",
    "bundled_bin_dir",
    "ensure_bundled_bin_on_path",
]

_PLATFORM_MAP: dict[str, str] = {
    "win32": "windows",
    "linux": "linux",
    "darwin": "darwin",
}


def _platform_name() -> str:
    return _PLATFORM_MAP.get(sys.platform, sys.platform)


def prepend_path_idempotent(
    path_env: str,
    new_dir: str,
    marker: str | None = None,
    pathsep: str = os.pathsep,
) -> str:
    """Prepend *new_dir* to *path_env*, idempotently.

    If *marker* is provided: removes all entries whose lowercased form
    contains the lowercased marker before prepending.  This handles
    reinstall-to-different-dir — the old install's entry is cleaned.

    If *marker* is ``None``: removes exact (case-insensitive) matches of
    *new_dir* before prepending.  Sufficient for process-env idempotency
    (repeated calls within the same process).

    Either way the result contains exactly one *new_dir* entry, prepended.
    """
    entries = [e.strip() for e in path_env.split(pathsep) if e.strip()]

    if marker is not None:
        marker_lower = marker.lower()
        kept = [e for e in entries if marker_lower not in e.lower()]
    else:
        new_dir_lower = new_dir.lower()
        kept = [e for e in entries if e.lower() != new_dir_lower]

    if kept:
        return new_dir + pathsep + pathsep.join(kept)
    return new_dir


def bundled_bin_dir() -> Path | None:
    """Resolve the platform-specific bundled binary directory.

    Resolution priority (highest first):

    1. ``MODEX_BUNDLED_BIN_DIR`` env var — explicit override (test/pin).
    2. Walk up from ``sys.executable``'s parent (up to 4 ancestors) looking
       for ``<ancestor>/bin/<platform>/``.  Matches both the Windows installer
       layout (``<install>/python/python.exe`` → ``<install>/bin/windows/``)
       and a hypothetical POSIX layout
       (``<install>/python/bin/python`` → ``<install>/bin/linux/``).

    Returns ``None`` in dev mode (no bundled binaries exist).
    """
    env_dir = os.environ.get("MODEX_BUNDLED_BIN_DIR")
    if env_dir:
        p = Path(env_dir)
        return p if p.is_dir() else None

    platform = _platform_name()
    ancestor = Path(sys.executable).resolve().parent

    for _ in range(4):
        candidate = ancestor / "bin" / platform
        if candidate.is_dir():
            return candidate
        if ancestor == ancestor.parent:
            break
        ancestor = ancestor.parent

    return None


def ensure_bundled_bin_on_path() -> Path | None:
    """Idempotently prepend the bundled bin dir to ``os.environ["PATH"]``.

    Uses exact-match cleanup (not marker-based) because the private layer
    is process-env (transient, never persisted to the registry) — there's
    no reinstall-to-different-dir concern.

    Returns the injected directory, or ``None`` if no bundled dir exists
    (dev mode — the bot falls back to system ``rg``).
    """
    bin_dir = bundled_bin_dir()
    if bin_dir is None:
        return None

    bin_dir_str = str(bin_dir)
    current = os.environ.get("PATH", "")
    new_path = prepend_path_idempotent(current, bin_dir_str, marker=None)
    os.environ["PATH"] = new_path
    return bin_dir
