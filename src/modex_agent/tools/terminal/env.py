"""Build a complete environment dict for child processes.

On Windows, os.environ may lack HKLM System PATH entries when the Python
process was launched from certain parents (IDE, CMD, etc.).  This module
reads the registry and merges missing PATH entries so spawned child
processes can find all installed tools.

Additionally, the bundled CLI binary directory (``<install>/bin/<platform>/``)
is prepended to PATH so child processes (terminals, subprocess tools) find
the bundled ``rg`` before any system-installed copies.  This is the
**private layer** — it only affects the bot process and its children, never
the system PATH.  See :mod:`modex_agent.runtime.bundled_bin` for details.

On non-Windows platforms the registry merge is a transparent passthrough;
the bundled-bin prepend still applies when a bundled dir exists.
"""

from __future__ import annotations

import os
import sys

from modex_agent.runtime.bundled_bin import (
    bundled_bin_dir,
    prepend_path_idempotent,
)


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

    Three-stage PATH construction (highest priority first):

    1. **Bundled bin dir** — ``<install>/bin/<platform>/`` prepended
       idempotently via :func:`prepend_path_idempotent`.  Gives child
       processes (terminals, subprocess tools) access to the bundled
       ``rg``.  No-op in dev mode (no bundled dir → ``None``).
    2. **Process PATH** — ``os.environ["PATH"]`` as-is (already includes
       the bundled dir if :func:`ensure_bundled_bin_on_path` ran at startup).
    3. **Registry PATH** (Windows only) — HKLM System + HKCU User entries
       missing from the process PATH are appended (lowest priority).

    Sets ``PAGER=cat`` (and related overrides) so output is never trapped
    in interactive pagers like ``less`` or ``more``.  Agents cannot
    interact with pagers; redirecting paged output to stdout avoids hangs.

    Args:
        overrides: Optional dict of extra env vars (highest priority).
    """
    env = dict(os.environ)

    # Disable interactive pagers so agent commands never stall waiting for
    # ``q`` / Space / Enter in less/more.  Most CLI tools respect PAGER;
    # the supplemental vars catch tool-specific pager configs.
    env.setdefault("PAGER", "cat")
    env.setdefault("MANPAGER", "cat")
    env.setdefault("GIT_PAGER", "cat")

    # Stage 1: prepend bundled bin dir (idempotent — safe even if
    # ensure_bundled_bin_on_path() already ran at startup).
    bin_dir = bundled_bin_dir()
    if bin_dir is not None:
        current = env.get("PATH", "")
        env["PATH"] = prepend_path_idempotent(
            current, str(bin_dir), marker=None
        )

    # Stage 2 + 3: merge missing Windows registry PATH entries (append only).
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
