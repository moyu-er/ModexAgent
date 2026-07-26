"""Bundled CLI binary resolution and idempotent PATH injection.

Two-layer PATH strategy for the bot installer:

**Private layer** (bot-only, transient):
    Bundled ``rg`` at ``<install>/bin/<platform>/``.  Injected into
    the bot process's ``os.environ["PATH"]`` at startup and into every child
    process env built by :func:`modex_agent.tools.terminal.env.build_full_env`.
    Never written to the registry — disappears when the bot process exits.
    Uses exact-match cleanup (no marker) because process-env is transient:
    there's no reinstall-to-different-dir concern.

**Public layer** (all processes, persistent):
    ``modexbot``/``modexctl`` shims at ``<install>/python/Scripts/``.
    Registered into ``HKCU\\Environment\\Path`` by the installer
    (``postinstall.py``) so the CLI is available from any terminal.
    Uses a product-specific marker (e.g. ``\\ModexBot\\python\\Scripts``)
    so reinstall-to-different-dir cleans stale entries from the prior
    install without touching other products' ``python\\Scripts`` entries.

Both layers share :func:`prepend_path_idempotent` — the single pure
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
    "register_public_path",
    "unregister_public_path",
]

_IS_WINDOWS: bool = sys.platform == "win32"

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


def register_public_path(bin_dir: Path, marker: str) -> bool:
    """Register *bin_dir* in ``HKCU\\Environment\\Path`` (Windows, idempotent).

    Uses the provided *marker* to identify and remove stale entries from
    prior installs before prepending.  The marker should be product-specific
    (e.g. ``\\ModexBot\\python\\Scripts``) to avoid touching other products'
    PATH entries.

    On POSIX this is a no-op (returns ``False``).

    Args:
        bin_dir: The directory to register.
        marker: Product-specific substring identifying entries to clean.
    """
    if not _IS_WINDOWS:
        return False

    try:
        import winreg
    except ImportError:
        return False

    bin_dir_str = str(bin_dir)

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                current, _reg_type = winreg.QueryValueEx(key, "Path")
                current = str(current)
            except OSError:
                current = ""

            new_path = prepend_path_idempotent(current, bin_dir_str, marker=marker)
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)

        _broadcast_setting_change()
        return True
    except OSError:
        return False


def unregister_public_path(marker: str) -> bool:
    """Remove all marker-matching entries from ``HKCU\\Environment\\Path``.

    Used by the uninstaller.  The marker must match the one passed to
    :func:`register_public_path`.

    On POSIX this is a no-op (returns ``False``).
    """
    if not _IS_WINDOWS:
        return False

    try:
        import winreg
    except ImportError:
        return False

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                current, _reg_type = winreg.QueryValueEx(key, "Path")
                current = str(current)
            except OSError:
                return False

            marker_lower = marker.lower()
            entries = [e.strip() for e in current.split(os.pathsep) if e.strip()]
            kept = [e for e in entries if marker_lower not in e.lower()]
            new_path = os.pathsep.join(kept)

            if new_path == current:
                return False

            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)

        _broadcast_setting_change()
        return True
    except OSError:
        return False


def _broadcast_setting_change() -> None:
    """Broadcast ``WM_SETTINGCHANGE`` so new processes pick up PATH changes.

    Windows-only.  Passes ``"Environment"`` as the lParam so listening
    processes (Explorer, new shells) actually refresh their env copy.
    Uses ``SendMessageTimeoutW`` with ``SMTO_ABORTIFHUNG`` to avoid
    blocking if a receiver is unresponsive.
    """
    if not _IS_WINDOWS:
        return

    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        ctypes.c_wchar_p("Environment"),
        SMTO_ABORTIFHUNG,
        1000,
        ctypes.byref(result),
    )
