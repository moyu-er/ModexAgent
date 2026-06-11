"""Persistent storage for terminal state (JSON)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonTerminalStateStore:
    """Stores terminal session metadata and history to a JSON file.

    Note: Does NOT serialize the actual process — only metadata.
    Sessions are lazily restarted on first use after load.
    """

    def __init__(self, storage_dir: Path, filename: str = "state.json") -> None:
        self._storage_dir = Path(storage_dir)
        self._file_path = self._storage_dir / filename

    def save(self, state: dict[str, Any]) -> None:
        """Save state to JSON file."""
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to save terminal state")

    def load(self) -> dict[str, Any]:
        """Load state from JSON file.

        Returns empty dict if file missing or corrupted.
        """
        if not self._file_path.exists():
            return {}
        try:
            with open(self._file_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning("Terminal state file corrupted, starting fresh")
            return {}
