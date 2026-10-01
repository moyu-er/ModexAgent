"""Windows-registry public PATH layer — installer-only.

**Public layer** (all processes, persistent):
    ``modexbot``/``modexctl`` shims at ``<install>/python/Scripts/``.
    Registered into ``HKCU\\Environment\\Path`` by the installer
    (``postinstall.py``) so the CLI is available from any terminal.
    Uses a product-specific marker (e.g. ``\\ModexBot\\python\\Scripts``)
    so reinstall-to-different-dir cleans stale entries from the prior
    install without touching other products' ``python\\Scripts`` entries.

Moved out of the framework (W2 bundled-bin split): the registry /
``WM_SETTINGCHANGE`` concerns are installer-domain, not framework runtime;
the shared pure algorithm (:func:`prepend_path_idempotent`) stays in the
framework at ``modex_agent.tools.terminal.bundled_bin``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from modex_agent.utils.bundled_bin import prepend_path_idempotent

__all__ = [
    "register_public_path",
    "unregister_public_path",
]

# Win32 SDK constants for the WM_SETTINGCHANGE broadcast (values fixed by
# the Windows API; names kept in SDK spelling).
_HWND_BROADCAST = 0xFFFF
_WM_SETTINGCHANGE = 0x001A
_SMTO_ABORTIFHUNG = 0x0002


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
    if sys.platform != "win32":
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
    if sys.platform != "win32":
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
    if sys.platform != "win32":
        return

    import ctypes

    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        _HWND_BROADCAST,
        _WM_SETTINGCHANGE,
        0,
        ctypes.c_wchar_p("Environment"),
        _SMTO_ABORTIFHUNG,
        1000,
        ctypes.byref(result),
    )
