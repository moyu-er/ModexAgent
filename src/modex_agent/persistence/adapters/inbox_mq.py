"""SQLite-backed ``InboxMQ`` adapter (T20).

Implements the full :class:`~modex_agent.multi_agent.inbox.server.InboxMQ`
contract against the workspace DB schema (T06):

- **Async surface** (``receive``/``consume``/``peek``/``count``/``clear``/
  ``sessions_with_pending``/``wakeup``/``wait_wakeup``/``reap_expired``) goes
  through :class:`~modex_agent.persistence.connection.ConnectionManager` —
  one shared aiosqlite connection serialized by an operation lock.

- **Sync ``deliver``** is path-owned: it opens its own short-lived stdlib
  :mod:`sqlite3` connection (``BEGIN IMMEDIATE`` … ``COMMIT`` … ``close``) and
  **never** reuses the async ``ConnectionManager``'s connection. This is the
  only method safe to call from non-async (CLI) code; it works even when
  ``connection`` is ``None`` (CLI mode).

Idempotency is enforced at the DB level via ``UNIQUE(scope_key, message_id)``
on ``inbox_messages`` (``ON CONFLICT DO NOTHING``) plus a separate
``inbox_delivered_ids`` table that survives ``clear()``-style row deletion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.inbox.server import InboxMQ
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.persistence.connection import (
    ConnectionManager,
    ConnectionNotOpenError,
    Transaction,
)

logger = logging.getLogger(__name__)

__all__ = ["SqliteInboxMQ"]


class SqliteInboxMQ(InboxMQ):
    """SQLite-backed ``InboxMQ`` using ``ConnectionManager`` + workspace schema.

    Args:
        db_path: Path to the workspace SQLite DB file. Used by the sync
            ``deliver()`` to open its own short-lived stdlib ``sqlite3``
            connection.
        connection: ``ConnectionManager`` for async methods. ``None`` for
            CLI-only use (only ``deliver()`` is callable).
        scope: Typed owner scope used to derive canonical inbox identities.
        message_ttl_seconds: Optional TTL for ``reap_expired()``. ``None``
            disables reaping (returns ``0``).
    """

    def __init__(
        self,
        db_path: Path,
        scope: RecordScope,
        *,
        connection: ConnectionManager | None = None,
        message_ttl_seconds: float | None = None,
    ) -> None:
        self._db_path = db_path
        self._connection = connection
        self._scope = scope
        self._owner_scope_key = scope.canonical()
        self._message_ttl_seconds = message_ttl_seconds
        self._wakeup_events: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------ #
    # Async MQ surface (server-side, framework process)
    # ------------------------------------------------------------------ #

    async def receive(self, session_id: str, message: InboxMessage) -> bool:
        """Idempotent intake within this inbox's exact session scope."""
        conn = self._require_connection()
        scope_key = self._scope_key(session_id)
        now = time.time()

        async with conn.transaction(immediate=True) as tx:
            # Already consumed — permanent dedup via delivered_ids.
            delivered = await tx.query_one(
                "SELECT 1 FROM inbox_delivered_ids "
                "WHERE scope_key = ? AND message_id = ?",
                (scope_key, message.message_id),
            )
            if delivered is not None:
                return False

            await self._ensure_topic(tx, session_id, scope_key, now)
            inserted = await self._insert_message(tx, session_id, scope_key, message, now)
            return inserted

    async def consume(
        self,
        session_id: str,
        limit: int = 100,
        *,
        only_types: set[str] | None = None,
    ) -> list[InboxMessage]:
        """Atomic FIFO consume: state→consumed + delivered_id in one transaction."""
        conn = self._require_connection()
        scope_key = self._scope_key(session_id)

        if only_types is not None and len(only_types) == 0:
            return []

        async with conn.transaction(immediate=True) as tx:
            rows = await self._select_pending(tx, scope_key, limit, only_types)
            if not rows:
                return []

            now = time.time()
            messages: list[InboxMessage] = []
            for row in rows:
                await tx.execute(
                    "UPDATE inbox_messages SET state = 'consumed', consumed_at = ? "
                    "WHERE scope_key = ? AND id = ?",
                    (now, scope_key, row["id"]),
                )
                await tx.execute(
                    "INSERT INTO inbox_delivered_ids "
                    "(owner_scope_key, scope_key, session_id, message_id, delivered_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (
                        self._owner_scope_key,
                        scope_key,
                        session_id,
                        row["message_id"],
                        now,
                    ),
                )
                messages.append(self._row_to_message(row))

            await tx.execute(
                "UPDATE inbox_topics SET state = 'active', last_active = ? "
                "WHERE scope_key = ?",
                (now, scope_key),
            )
            return messages

    async def peek(self, session_id: str) -> list[InboxMessage]:
        """Non-destructive read of the pending queue."""
        conn = self._require_connection()
        scope_key = self._scope_key(session_id)
        rows = await conn.query_all(
            "SELECT * FROM inbox_messages "
            "WHERE scope_key = ? AND state = 'pending' ORDER BY seq",
            (scope_key,),
        )
        return [self._row_to_message(row) for row in rows]

    async def count(self, session_id: str) -> int:
        """Return the number of pending messages for ``session_id``."""
        conn = self._require_connection()
        scope_key = self._scope_key(session_id)
        return await conn.query_value(
            "SELECT COUNT(*) FROM inbox_messages "
            "WHERE scope_key = ? AND state = 'pending'",
            int,
            (scope_key,),
        )

    async def clear(self, session_id: str) -> None:
        """Clear pending messages and delivered-id records for ``session_id``."""
        conn = self._require_connection()
        scope_key = self._scope_key(session_id)
        async with conn.transaction(immediate=True) as tx:
            await tx.execute(
                "DELETE FROM inbox_messages WHERE scope_key = ?",
                (scope_key,),
            )
            await tx.execute(
                "DELETE FROM inbox_delivered_ids WHERE scope_key = ?",
                (scope_key,),
            )
            await tx.execute(
                "UPDATE inbox_topics SET state = 'idle', message_count = 0, "
                "last_active = ? WHERE scope_key = ?",
                (time.time(), scope_key),
            )

    async def sessions_with_pending(self) -> list[str]:
        """Return session ids with at least one pending message."""
        conn = self._require_connection()
        rows = await conn.query_all(
            "SELECT DISTINCT session_id FROM inbox_messages "
            "WHERE owner_scope_key = ? AND state = 'pending'",
            (self._owner_scope_key,),
        )
        return [row[0] for row in rows]

    async def list_sessions(self) -> list[str]:
        """Return all known session ids (pending + consumed)."""
        conn = self._require_connection()
        rows = await conn.query_all(
            "SELECT DISTINCT session_id FROM inbox_messages WHERE owner_scope_key = ?",
            (self._owner_scope_key,),
        )
        return [row[0] for row in rows]

    # ------------------------------------------------------------------ #
    # Sync delivery surface (CLI cross-process)
    # ------------------------------------------------------------------ #

    def deliver(self, session_id: str, message: InboxMessage) -> bool:
        """Sync cross-process delivery — stdlib ``sqlite3``, own connection.

        Opens a short-lived ``sqlite3`` connection with ``BEGIN IMMEDIATE``,
        upserts the topic, inserts the message with
        ``ON CONFLICT(scope_key, message_id) DO NOTHING``, commits, and
        closes. Never reuses the async ``ConnectionManager``'s connection.
        """
        scope_key = self._scope_key(session_id)
        now = time.time()
        source_kind = message.metadata.get("source_kind", "agent")
        payload_json = json.dumps(message.metadata) if message.metadata else None

        conn = sqlite3.connect(str(self._db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")

            # Already consumed — permanent dedup.
            row = conn.execute(
                "SELECT 1 FROM inbox_delivered_ids "
                "WHERE scope_key = ? AND message_id = ?",
                (scope_key, message.message_id),
            ).fetchone()
            if row is not None:
                conn.execute("COMMIT")
                return False

            # Upsert topic.
            conn.execute(
                "INSERT INTO inbox_topics "
                "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope_key) DO NOTHING",
                (self._owner_scope_key, scope_key, session_id, scope_key, now, now),
            )
            topic_id = conn.execute(
                "SELECT topic_id FROM inbox_topics WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()[0]

            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM inbox_messages "
                "WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()[0]

            cursor = conn.execute(
                "INSERT INTO inbox_messages "
                "(topic_id, owner_scope_key, scope_key, session_id, scope, "
                " message_id, message_type, "
                " source_name, source_kind, content, payload_json, "
                " envelope_session_id, envelope_agent_session_id, "
                " state, seq, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(scope_key, message_id) DO NOTHING",
                (
                    topic_id,
                    self._owner_scope_key,
                    scope_key,
                    session_id,
                    scope_key,
                    message.message_id,
                    message.message_type,
                    message.source,
                    source_kind,
                    message.content,
                    payload_json,
                    message.metadata.get("session_id"),
                    message.metadata.get("agent_session_id"),
                    seq,
                    message.timestamp.timestamp(),
                ),
            )
            if cursor.rowcount == 0:
                conn.execute("COMMIT")
                return False

            conn.execute(
                "UPDATE inbox_topics SET last_active = ?, message_count = message_count + 1 "
                "WHERE scope_key = ?",
                (now, scope_key),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Wakeup surface (poller latency reduction)
    # ------------------------------------------------------------------ #

    async def wakeup(self, session_id: str) -> None:
        """Signal that ``session_id`` has pending work (in-process event)."""
        self._get_wakeup_event(session_id).set()

    async def wait_wakeup(
        self,
        session_id: str,
        timeout: float | None = None,
    ) -> bool:
        """Wait for a wakeup signal. Returns ``True`` if woken, ``False`` on timeout."""
        event = self._get_wakeup_event(session_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            event.clear()
            return True
        except TimeoutError:
            return False

    # ------------------------------------------------------------------ #
    # Lifecycle maintenance
    # ------------------------------------------------------------------ #

    async def reap_expired(self) -> int:
        """Delete expired messages and stale delivered-id records (TTL)."""
        conn = self._require_connection()
        if self._message_ttl_seconds is None:
            return 0

        cutoff = time.time() - self._message_ttl_seconds
        async with conn.transaction(immediate=True) as tx:
            await tx.execute(
                "DELETE FROM inbox_messages "
                "WHERE owner_scope_key = ? AND state = 'expired' AND created_at < ?",
                (self._owner_scope_key, cutoff),
            )
            msg_deleted = await tx.query_value("SELECT changes()", int)
            await tx.execute(
                "DELETE FROM inbox_delivered_ids "
                "WHERE owner_scope_key = ? AND delivered_at < ?",
                (self._owner_scope_key, cutoff),
            )
            ids_deleted = await tx.query_value("SELECT changes()", int)
            return msg_deleted + ids_deleted

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _require_connection(self) -> ConnectionManager:
        if self._connection is None:
            raise ConnectionNotOpenError(
                "SqliteInboxMQ async methods require a ConnectionManager; "
                "only deliver() is available in CLI mode (connection=None)."
            )
        return self._connection

    def _get_wakeup_event(self, session_id: str) -> asyncio.Event:
        event = self._wakeup_events.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._wakeup_events[session_id] = event
        return event

    def _scope_key(self, session_id: str) -> str:
        session_scope = self._scope.model_copy(update={"session_id": session_id})
        return session_scope.canonical()

    async def _ensure_topic(
        self,
        tx: Transaction,
        session_id: str,
        scope_key: str,
        now: float,
    ) -> None:
        await tx.execute(
            "INSERT INTO inbox_topics "
            "(owner_scope_key, scope_key, session_id, scope, created_at, last_active) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(scope_key) DO NOTHING",
            (self._owner_scope_key, scope_key, session_id, scope_key, now, now),
        )

    async def _insert_message(
        self,
        tx: Transaction,
        session_id: str,
        scope_key: str,
        message: InboxMessage,
        now: float,
    ) -> bool:
        topic_id = await tx.query_value(
            "SELECT topic_id FROM inbox_topics WHERE scope_key = ?",
            int,
            (scope_key,),
        )
        seq = await tx.query_value(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM inbox_messages "
            "WHERE scope_key = ?",
            int,
            (scope_key,),
        )
        source_kind = message.metadata.get("source_kind", "agent")
        payload_json = json.dumps(message.metadata) if message.metadata else None

        await tx.execute(
            "INSERT INTO inbox_messages "
            "(topic_id, owner_scope_key, scope_key, session_id, scope, "
            " message_id, message_type, "
            " source_name, source_kind, content, payload_json, "
            " envelope_session_id, envelope_agent_session_id, "
            " state, seq, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
            "ON CONFLICT(scope_key, message_id) DO NOTHING",
            (
                topic_id,
                self._owner_scope_key,
                scope_key,
                session_id,
                scope_key,
                message.message_id,
                message.message_type,
                message.source,
                source_kind,
                message.content,
                payload_json,
                message.metadata.get("session_id"),
                message.metadata.get("agent_session_id"),
                seq,
                message.timestamp.timestamp(),
            ),
        )
        changes = await tx.query_value("SELECT changes()", int)
        if changes > 0:
            await tx.execute(
                "UPDATE inbox_topics SET last_active = ?, "
                "message_count = message_count + 1 WHERE scope_key = ?",
                (now, scope_key),
            )
            return True
        return False

    async def _select_pending(
        self,
        tx: Transaction,
        scope_key: str,
        limit: int,
        only_types: set[str] | None,
    ) -> list[sqlite3.Row]:
        if only_types is None:
            return await tx.query_all(
                "SELECT * FROM inbox_messages "
                "WHERE scope_key = ? AND state = 'pending' "
                "ORDER BY seq LIMIT ?",
                (scope_key, limit),
            )
        placeholders = ",".join("?" * len(only_types))
        types_tuple = tuple(only_types)
        return await tx.query_all(
            f"SELECT * FROM inbox_messages "
            f"WHERE scope_key = ? AND state = 'pending' "
            f"AND message_type IN ({placeholders}) "
            f"ORDER BY seq LIMIT ?",
            (scope_key, *types_tuple, limit),
        )

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> InboxMessage:
        """Reconstruct an ``InboxMessage`` from a DB row."""
        metadata: dict[str, Any] = {}
        payload = row["payload_json"]
        if payload:
            metadata = json.loads(payload)
        timestamp = datetime.fromtimestamp(row["created_at"], tz=UTC)
        return InboxMessage(
            session_id=row["session_id"],
            source=row["source_name"],
            content=row["content"],
            message_type=row["message_type"],
            message_id=row["message_id"],
            timestamp=timestamp,
            metadata=metadata,
        )
