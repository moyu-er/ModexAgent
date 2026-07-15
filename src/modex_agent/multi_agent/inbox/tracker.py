"""Delivered ID Tracker — **deprecated** ABC + file implementation (T11).

PRD story 23 merges delivered-id tracking into :class:`InboxMQ` internal:
dedup is part of the inbox transaction, not a standalone ABC. The
:class:`DeliveredIdTracker` ABC is kept here only as a **deprecated** seam
for backwards compatibility; new code must not depend on it.

:class:`FileDeliveredIdTracker` remains as an internal helper used by
:class:`~modex_agent.multi_agent.inbox.server_local.LocalFileInboxMQ`. It is
not part of the public ``InboxMQ`` surface.
"""

from __future__ import annotations

import json
import warnings
from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.utils.file_io import read_json_robust

MAX_DELIVERED_IDS = 10000


class DeliveredIdTracker(ABC):
    """**Deprecated** — delivered-id tracking is now internal to :class:`InboxMQ`.

    This ABC is preserved solely so existing imports and the file backend's
    internal helper continue to type-check. New code must not subclass or
    depend on it; delivered-id dedup is owned by each :class:`InboxMQ`
    implementation's consume/deliver transaction.
    """

    def __init__(self) -> None:
        warnings.warn(
            "DeliveredIdTracker is deprecated; delivered-id tracking is now "
            "internal to InboxMQ (T11/PRD story 23).",
            DeprecationWarning,
            stacklevel=2,
        )

    @abstractmethod
    async def load(self, session_id: str) -> set[str]:
        """Load the delivered-id set for ``session_id``."""
        ...

    @abstractmethod
    async def save(self, session_id: str, ids: set[str]) -> None:
        """Persist the delivered-id set for ``session_id``."""
        ...

    @abstractmethod
    async def add(self, session_id: str, message_id: str) -> None:
        """Add a single delivered id and persist."""
        ...

    @abstractmethod
    async def clear(self, session_id: str) -> None:
        """Clear the delivered-id records for ``session_id``."""
        ...


class FileDeliveredIdTracker(DeliveredIdTracker):
    """File-based delivered-id tracker — internal helper for ``LocalFileInboxMQ``.

    Not part of the public ``InboxMQ`` contract. Retained as a private
    implementation detail of the file backend so the file MQ can compose it
    without re-implementing the JSON load/save/LRU logic.
    """

    def __init__(self, workspace: Path, max_ids: int = MAX_DELIVERED_IDS) -> None:
        # Skip the deprecated base-class warning: this is the internal helper
        # that LocalFileInboxMQ legitimately uses, not a public-tracker user.
        # We intentionally do NOT call super().__init__().
        self._workspace = Path(workspace)
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._max_ids = max_ids

    def _session_dir(self, session_id: str) -> Path:
        from .server_local import _safe_dir_name

        return self._workspace / _safe_dir_name(session_id)

    def _delivered_path(self, session_dir: Path) -> Path:
        return session_dir / "delivered_ids.json"

    def _load(self, delivered_path: Path) -> set[str]:
        data = read_json_robust(delivered_path)
        if not data:
            return set()
        return set(data.get("ids", []))

    def _save(self, delivered_path: Path, ids: set[str]) -> None:
        delivered_path.parent.mkdir(parents=True, exist_ok=True)
        delivered_path.write_text(
            json.dumps({"ids": list(ids)}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def load(self, session_id: str) -> set[str]:
        return self._load(self._delivered_path(self._session_dir(session_id)))

    async def save(self, session_id: str, ids: set[str]) -> None:
        delivered_path = self._delivered_path(self._session_dir(session_id))
        if len(ids) > self._max_ids:
            ids = set(list(ids)[-self._max_ids :])
        self._save(delivered_path, ids)

    async def add(self, session_id: str, message_id: str) -> None:
        delivered_path = self._delivered_path(self._session_dir(session_id))
        ids = self._load(delivered_path)
        ids.add(message_id)
        if len(ids) > self._max_ids:
            ids = set(list(ids)[-self._max_ids :])
        self._save(delivered_path, ids)

    async def clear(self, session_id: str) -> None:
        delivered_path = self._delivered_path(self._session_dir(session_id))
        if delivered_path.exists():
            delivered_path.write_text(
                json.dumps({"ids": []}, ensure_ascii=False), encoding="utf-8"
            )
