"""Recent workspace tracking — convenience for workspace switching.

.. deprecated:: T14
    Use :meth:`modex_agent.workspace.registry.WorkspaceRegistryStore.list_workspaces`
    with ``order_by="last_active"`` instead.  This module is retained for
    backward compatibility until T23 removes it.

Maintains a JSON file of recently visited workspace paths (max 20),
used by the WebUI to offer a quick-switch dropdown so users don't need
to re-browse the filesystem every time.

The file lives in the **project home** ``.modex/`` directory (NOT per-workspace)
because it tracks which workspaces have been visited, not data owned by any
single workspace.
"""

from __future__ import annotations

import json
import os
import time
import warnings
from pathlib import Path

RECENT_FILE: str = "recent_workspaces.json"
MAX_RECENT: int = 20


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class RecentWorkspaces:
    """Maintains a max-20 list of recently visited workspace paths.

    .. deprecated:: T14
        Superseded by
        :class:`modex_agent.workspace.registry.WorkspaceRegistryStore.list_workspaces`.
        T23 will remove this class.

    Paths are deduplicated; the most recently visited path appears first.
    """

    def __init__(self, data_dir: Path) -> None:
        warnings.warn(
            "RecentWorkspaces is deprecated since T14; use "
            "WorkspaceRegistryStore.list_workspaces(order_by='last_active') "
            "instead. T23 will remove this class.",
            DeprecationWarning,
            stacklevel=2,
        )
        self._path = data_dir / RECENT_FILE
        self._recent: list[dict[str, object]] = []

    def load(self) -> None:
        data = _read_json(self._path)
        raw = data.get("recent")
        if isinstance(raw, list):
            self._recent = [r for r in raw if isinstance(r, dict) and "path" in r]

    def _save(self) -> None:
        _write_atomic(self._path, {"recent": self._recent})

    def list_recent(self) -> list[dict[str, object]]:
        return list(self._recent)

    def add(self, workspace_path: str) -> None:
        """Record *workspace_path* as the most recently visited.

        Moves an existing entry to the front, or creates a new one.
        The list is capped at ``MAX_RECENT``.
        """
        now_ms = int(time.time() * 1000)
        # Remove existing entry for this path (dedup)
        self._recent = [r for r in self._recent if r.get("path") != workspace_path]
        # Insert at front
        self._recent.insert(0, {"path": workspace_path, "last_used": now_ms})
        # Trim
        self._recent = self._recent[:MAX_RECENT]
        self._save()

    def remove(self, workspace_path: str) -> bool:
        """Remove *workspace_path* from the recent list.

        Returns True if the path was found and removed.
        """
        before = len(self._recent)
        self._recent = [r for r in self._recent if r.get("path") != workspace_path]
        if len(self._recent) < before:
            self._save()
            return True
        return False
