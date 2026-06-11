"""Build a complete environment dict for child processes.

On Windows, os.environ may lack HKLM System PATH entries when the Python
process was launched from certain parents (IDE, CMD, etc.).  This module
reads the registry and merges missing PATH entries so spawned child
processes can find all installed tools.

On non-Windows platforms this is a transparent passthrough.
"""

from __future__ import annotations

import os
import sys


def _registry_path_entries() -> list[str]:
    """Collect PATH entries from Windows registry (HKLM System + HKCU User)."""
    if sys.platform != "win32":
        return []

    import winreg

    entries: list[str] = []
    for hive, subkey in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, r"Environment"),
    ):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                path_val, _ = winreg.QueryValueEx(key, "Path")
                if isinstance(path_val, str):
                    entries.extend(path_val.split(os.pathsep))
        except OSError:
            continue

    return entries


def build_full_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return a complete environment dict for spawning child processes.

    On Windows, merges HKLM System PATH and HKCU User PATH entries that
    are missing from os.environ into a copy of os.environ.  Process PATH
    entries keep their position (priority); registry entries are appended
    only if absent.

    On non-Windows, returns dict(os.environ) unchanged.

    Args:
        overrides: Optional dict of extra env vars (highest priority).
    """
    env = dict(os.environ)

    if sys.platform == "win32":
        current = env.get("PATH", "")
        current_set = set(current.split(os.pathsep))

        missing: list[str] = []
        for entry in _registry_path_entries():
            entry = entry.strip()
            if entry and entry not in current_set:
                current_set.add(entry)
                missing.append(entry)

        if missing:
            sep = os.pathsep if current else ""
            env["PATH"] = current + sep + os.pathsep.join(missing)

    if overrides:
        env.update(overrides)

    return env
