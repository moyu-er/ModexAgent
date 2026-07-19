"""SQLite-backed ``InboxMQ`` adapter (T20).

Implements the full :class:`~modex_agent.multi_agent.inbox.server.InboxMQ`
contract against the workspace DB schema (T06 / ADR-0031):

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

Schema notes (ADR-0031):

- ``inbox_topics`` is insert-once-delete-on-cleanup — no state machine, no
  ``last_active``/``message_count`` maintenance.
- ``inbox_messages`` carries only the projection-extracted columns
  (``message_id``/``message_type``/``session_id``) plus ``payload_json``
  (the full ``InboxMessage`` dict). The 5 dead business columns
  (``source_name``/``source_kind``/``content``/``envelope_session_id``/
  ``envelope_agent_session_id``) are gone.
- ``inbox_delivered_ids`` FK is single-column ``scope_key`` (no ``session_id``).
- ``InboxMessage.timestamp`` (datetime) ↔ ``created_at`` (int ms) conversion
  happens at this adapter boundary.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modex_agent.core.scope import RecordScope
from modex_agent.multi_agent.inbox.server import InboxMQ
from modex_agent.multi_agent.inbox.types import InboxMessage
from modex_agent.persistence.column_projection import (
    ColumnField,
    ColumnProjection,
)
from modex_agent.persistence.connection import (
    ConnectionManager,
    ConnectionNotOpenError,
    Transaction,
)
from modex_agent.utils.time import now_ms

logger = logging.getLogger(__name__)

__all__ = ["SqliteInboxMQ"]

#: ColumnProjection for ``inbox_messages``: extract ``message_id`` /
#: ``message_type`` / ``session_id`` to indexed columns; store the residual
#: ``InboxMessage`` dict (``source``/``content``/``timestamp`` int ms /
#: ``metadata``) in ``payload_json``.
_INBOX_PROJECTION = ColumnProjection(
    fields=(
        ColumnField(column="message_id", dict_keys=("message_id",)),
        ColumnField(column="message_type", dict_keys=("message_type",)),
        ColumnField(column="session_id", dict_keys=("session_id",)),
    ),
    json_column="payload_json",
)


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

        async with conn.transaction(immediate=True) as tx:
            # Already consumed — permanent dedup via delivered_ids.
            delivered = await tx.query_one(
                "SELECT 1 FROM inbox_delivered_ids "
                "WHERE scope_key = ? AND message_id = ?",
                (scope_key, message.message_id),
            )
            if delivered is not None:
                return False

            await self._ensure_topic(tx, session_id, scope_key)
            inserted = await self._insert_message(tx, session_id, scope_key, message)
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

            now = now_ms()
            messages: list[InboxMessage] = []
            for row in rows:
                await tx.execute(
                    "UPDATE inbox_messages SET state = 'consumed', consumed_at = ? "
                    "WHERE scope_key = ? AND id = ?",
                    (now, scope_key, row["id"]),
                )
                await tx.execute(
                    "INSERT INTO inbox_delivered_ids "
                    "(owner_scope_key, scope_key, message_id, delivered_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT DO NOTHING",
                    (
                        self._owner_scope_key,
                        scope_key,
                        row["message_id"],
                        now,
                    ),
                )
                messages.append(self._row_to_message(row))

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
        ts_ms = int(message.timestamp.timestamp() * 1000)
        columns, payload_json = _INBOX_PROJECTION.split(
            self._message_dict(session_id, message, ts_ms)
        )

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

            # Upsert topic (insert-once; timestamps via DEFAULT).
            conn.execute(
                "INSERT INTO inbox_topics "
                "(owner_scope_key, scope_key, session_id) "
                "VALUES (?, ?, ?) ON CONFLICT(scope_key) DO NOTHING",
                (self._owner_scope_key, scope_key, session_id),
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
                "(topic_id, owner_scope_key, scope_key, session_id, "
                " message_id, message_type, payload_json, "
                " state, seq, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(scope_key, message_id) DO NOTHING",
                (
                    topic_id,
                    self._owner_scope_key,
                    scope_key,
                    columns["session_id"],
                    columns["message_id"],
                    columns["message_type"],
                    payload_json,
                    seq,
                    ts_ms,
                ),
            )
            if cursor.rowcount == 0:
                conn.execute("COMMIT")
                return False

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

        cutoff = now_ms() - int(self._message_ttl_seconds * 1000)
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

    @staticmethod
    def _message_dict(
        session_id: str,
        message: InboxMessage,
        ts_ms: int,
    ) -> dict[str, Any]:
        """Build the dict fed to ``_INBOX_PROJECTION.split()``.

        ``session_id`` is the receive-path parameter (the destination session);
        it overrides ``message.session_id`` so the column and the round-tripped
        ``InboxMessage.session_id`` match the API contract. ``timestamp`` is
        encoded as int ms to match the ``created_at`` column type.
        """
        return {
            "session_id": session_id,
            "source": message.source,
            "content": message.content,
            "message_type": message.message_type,
            "message_id": message.message_id,
            "timestamp": ts_ms,
            "metadata": message.metadata,
        }

    async def _ensure_topic(
        self,
        tx: Transaction,
        session_id: str,
        scope_key: str,
    ) -> None:
        await tx.execute(
            "INSERT INTO inbox_topics "
            "(owner_scope_key, scope_key, session_id) "
            "VALUES (?, ?, ?) ON CONFLICT(scope_key) DO NOTHING",
            (self._owner_scope_key, scope_key, session_id),
        )

    async def _insert_message(
        self,
        tx: Transaction,
        session_id: str,
        scope_key: str,
        message: InboxMessage,
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
        ts_ms = int(message.timestamp.timestamp() * 1000)
        columns, payload_json = _INBOX_PROJECTION.split(
            self._message_dict(session_id, message, ts_ms)
        )

        await tx.execute(
            "INSERT INTO inbox_messages "
            "(topic_id, owner_scope_key, scope_key, session_id, "
            " message_id, message_type, payload_json, "
            " state, seq, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) "
            "ON CONFLICT(scope_key, message_id) DO NOTHING",
            (
                topic_id,
                self._owner_scope_key,
                scope_key,
                columns["session_id"],
                columns["message_id"],
                columns["message_type"],
                payload_json,
                seq,
                ts_ms,
            ),
        )
        changes = await tx.query_value("SELECT changes()", int)
        return changes > 0

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
        row_dict = dict(row)
        assembled = _INBOX_PROJECTION.assemble(row_dict, row["payload_json"])
        timestamp = datetime.fromtimestamp(row["created_at"] / 1000, tz=UTC)
        return InboxMessage(
            session_id=assembled["session_id"],
            source=assembled["source"],
            content=assembled["content"],
            message_type=assembled["message_type"],
            message_id=assembled["message_id"],
            timestamp=timestamp,
            metadata=assembled.get("metadata") or {},
        )
