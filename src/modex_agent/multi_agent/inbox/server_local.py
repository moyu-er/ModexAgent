"""File-based ``InboxMQ`` implementation (T11).

Renamed from :class:`LocalFileInboxServer` to :class:`LocalFileInboxMQ`.
``LocalFileInboxServer`` is kept as a deprecated alias during the T11
transition.

The file backend is **deprecated but functional** for framework file-backend
users. It implements the full :class:`InboxMQ` contract:

- Async surface (``receive``/``consume``/``peek``/``count``/``clear``/
  ``sessions_with_pending``): unchanged from the legacy implementation; one
  ``asyncio.Lock`` per session for single-process safety.
- Sync :meth:`deliver`: writes directly to ``pending.jsonl`` (best-effort;
  cross-process atomicity is a known gap the SQLite backend closes).
- :meth:`reap_expired`: no-op (the file backend has no TTL; returns ``0``).

Delivered-id tracking is internal: the optional ``tracker`` parameter is
kept for backwards compatibility but new code should let the MQ create its
own :class:`FileDeliveredIdTracker`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from .server import InboxMQ
from .tracker import DeliveredIdTracker, FileDeliveredIdTracker
from .types import InboxMessage

logger = logging.getLogger(__name__)


def _safe_dir_name(session_id: str) -> str:
    safe = re.sub(r"[^\w\-.]", "_", session_id)
    if len(safe) > 200:
        import base64

        return base64.urlsafe_b64encode(session_id.encode()).decode()[:200]
    return safe


def _parse_inbox_timestamp(value: object) -> datetime:
    """Parse a ``pending.jsonl`` timestamp into a timezone-aware datetime.

    T9 switched :class:`LocalFileInboxMQ`'s own writes to int ms, but the
    ``modexctl send`` CLI (via :class:`OutboxLine`) still serialises
    ``timestamp`` as an ISO-8601 string until a follow-up ticket aligns it.
    The reader therefore accepts both representations.
    """
    if isinstance(value, int):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"unsupported inbox timestamp type: {type(value).__name__}")


class LocalFileInboxMQ(InboxMQ):
    """File-based ``InboxMQ`` implementation.

    Storage layout::

        {workspace}/{safe_session_id}/
            ├── pending.jsonl
            └── delivered_ids.json

    Concurrency: one ``asyncio.Lock`` per session (single-process safety).
    Cross-process writes via :meth:`deliver` are best-effort.
    """

    def __init__(
        self,
        workspace: Path,
        tracker: DeliveredIdTracker | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tracker = tracker or FileDeliveredIdTracker(workspace)

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _session_dir(self, session_id: str) -> Path:
        return self._workspace / _safe_dir_name(session_id)

    def _pending_path(self, session_dir: Path) -> Path:
        return session_dir / "pending.jsonl"

    # ------------------------------------------------------------------ #
    # Async MQ surface
    # ------------------------------------------------------------------ #

    async def receive(self, session_id: str, message: InboxMessage) -> bool:
        """Idempotent intake: check pending + delivered_ids, append if new."""
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)

        async with self._get_lock(session_id):
            delivered_ids = await self._tracker.load(session_id)
            if message.message_id in delivered_ids:
                return False

            if pending_path.exists():
                text = pending_path.read_text(encoding="utf-8")
                for line in text.strip().split("\n"):
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("message_id") == message.message_id:
                        return False

            session_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "message_id": message.message_id,
                    "source": message.source,
                    "content": message.content,
                    "message_type": message.message_type,
                    "timestamp": int(message.timestamp.timestamp() * 1000),
                    "metadata": message.metadata,
                },
                ensure_ascii=False,
            )
            with open(pending_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        """Atomic consume: read pending, write delivered_ids, keep remainder."""
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)

        async with self._get_lock(session_id):
            if not pending_path.exists():
                return []

            text = pending_path.read_text(encoding="utf-8")
            lines = [l for l in text.strip().split("\n") if l.strip()]

            if only_types is None:
                consume_lines = lines[:limit]
                remain_lines = lines[limit:]
            else:
                consume_lines: list[str] = []
                remain_lines: list[str] = []
                for line in lines:
                    data = json.loads(line)
                    if len(consume_lines) < limit and data.get("message_type") in only_types:
                        consume_lines.append(line)
                    else:
                        remain_lines.append(line)

            pending_path.write_text(
                "\n".join(remain_lines) + "\n" if remain_lines else "",
                encoding="utf-8",
            )

            delivered_ids = await self._tracker.load(session_id)

            messages = []
            for line in consume_lines:
                data = json.loads(line)
                messages.append(
                    InboxMessage(
                        session_id=session_id,
                        source=data["source"],
                        content=data["content"],
                        message_type=data["message_type"],
                        message_id=data["message_id"],
                        timestamp=_parse_inbox_timestamp(data["timestamp"]),
                        metadata=data.get("metadata", {}),
                    )
                )
                delivered_ids.add(data["message_id"])

            await self._tracker.save(session_id, delivered_ids)

        return messages

    async def peek(self, session_id: str) -> list[InboxMessage]:
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)
        if not pending_path.exists():
            return []
        async with self._get_lock(session_id):
            text = pending_path.read_text(encoding="utf-8")
        messages = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            messages.append(
                InboxMessage(
                    session_id=session_id,
                    source=data["source"],
                    content=data["content"],
                    message_type=data["message_type"],
                    message_id=data["message_id"],
                    timestamp=_parse_inbox_timestamp(data["timestamp"]),
                    metadata=data.get("metadata", {}),
                )
            )
        return messages

    async def count(self, session_id: str) -> int:
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)
        if not pending_path.exists():
            return 0
        async with self._get_lock(session_id):
            text = pending_path.read_text(encoding="utf-8")
        return sum(1 for line in text.split("\n") if line.strip())

    async def clear(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)
        async with self._get_lock(session_id):
            if pending_path.exists():
                pending_path.write_text("", encoding="utf-8")
            await self._tracker.clear(session_id)

    async def list_sessions(self) -> list[str]:
        """Scan workspace, return all sessions with a ``pending.jsonl``."""
        sessions = []
        for item in self._workspace.iterdir():
            if not item.is_dir():
                continue
            pending_path = item / "pending.jsonl"
            if not pending_path.exists():
                continue
            try:
                text = pending_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            sessions.append(
                self._session_id_from_text(text) or self._unsafe_dir_name(item.name)
            )
        return sessions

    async def sessions_with_pending(self) -> list[str]:
        """Scan workspace, return sessions with non-empty ``pending.jsonl``."""
        sessions = []
        for item in self._workspace.iterdir():
            if not item.is_dir():
                continue
            pending_path = item / "pending.jsonl"
            if not pending_path.exists():
                continue
            text = pending_path.read_text(encoding="utf-8")
            if not any(line.strip() for line in text.split("\n")):
                continue
            sessions.append(
                self._session_id_from_text(text) or self._unsafe_dir_name(item.name)
            )
        return sessions

    # ------------------------------------------------------------------ #
    # Sync delivery surface (CLI cross-process)
    # ------------------------------------------------------------------ #

    def deliver(self, session_id: str, message: InboxMessage) -> bool:
        """Sync cross-process delivery — writes directly to ``pending.jsonl``.

        Best-effort: the file backend does not own a DB transaction; cross-
        process atomicity is a known gap the SQLite backend closes (T20).
        Idempotency is checked against the pending file and the delivered-id
        store (both read synchronously).
        """
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)

        # Sync read of delivered_ids.json
        delivered_path = session_dir / "delivered_ids.json"
        delivered_ids: set[str] = set()
        if delivered_path.exists():
            try:
                data = json.loads(delivered_path.read_text(encoding="utf-8"))
                delivered_ids = set(data.get("ids", []))
            except (json.JSONDecodeError, OSError):
                delivered_ids = set()

        if message.message_id in delivered_ids:
            return False

        # Sync read of pending.jsonl for duplicate check
        if pending_path.exists():
            text = pending_path.read_text(encoding="utf-8")
            for line in text.strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("message_id") == message.message_id:
                    return False

        # Append
        session_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "message_id": message.message_id,
                "source": message.source,
                "content": message.content,
                "message_type": message.message_type,
                "timestamp": int(message.timestamp.timestamp() * 1000),
                "metadata": message.metadata,
            },
            ensure_ascii=False,
        )
        with open(pending_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True

    # ------------------------------------------------------------------ #
    # Lifecycle maintenance
    # ------------------------------------------------------------------ #

    async def reap_expired(self) -> int:
        """No-op for the file backend (no TTL policy). Returns ``0``."""
        return 0

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _session_id_from_text(self, text: str) -> str | None:
        """Recover the original session_id from the first pending record."""
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                agent_session_id = metadata.get("agent_session_id")
                if isinstance(agent_session_id, str) and agent_session_id:
                    return agent_session_id
        return None

    def _unsafe_dir_name(self, safe_name: str) -> str:
        """Best-effort reverse of ``_safe_dir_name``."""
        if len(safe_name) >= 200:
            try:
                import base64

                return base64.urlsafe_b64decode(safe_name.encode()).decode()
            except Exception:
                pass
        return safe_name


# Deprecated alias — new code should use ``LocalFileInboxMQ``. Kept during
# the T11 transition so existing imports continue to work.
LocalFileInboxServer = LocalFileInboxMQ
