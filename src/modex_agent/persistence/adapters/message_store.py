"""SQLite-backed :class:`~modex_agent.memory.core.split_stores.MessageStore`.

Conversation message history with a per-row state machine:
``normal → pinned → soft_deleted → DELETE``.

- ``normal`` / ``pinned`` rows are returned by :meth:`load_messages`.
- :meth:`prune_messages` atomically soft-deletes (``state='soft_deleted'``,
  ``deleted_at`` set) and returns the pruned content in one transaction.
- :meth:`cleanup_expired` physically deletes soft-deleted rows whose
  ``deleted_at`` is older than the configured TTL.

Revision metadata (``message_count``, ``version``, ``updated_at``) is tracked
in ``memory_revisions`` and bumped on every write.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.split_stores import MessageStore, message_signature

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager, Transaction

#: Default TTL for soft-deleted messages (7 days).
_DEFAULT_TTL_SECONDS: float = 7 * 24 * 3600


class MessageRowState(StrEnum):
    NORMAL = "normal"
    PINNED = "pinned"
    SOFT_DELETED = "soft_deleted"

    @classmethod
    def active(cls) -> str:
        """SQL fragment matching active (non-deleted) rows."""
        return f"'{cls.NORMAL.value}', '{cls.PINNED.value}'"

    @classmethod
    def all_visible(cls) -> str:
        """SQL fragment matching all rows including soft-deleted."""
        return f"'{cls.NORMAL.value}', '{cls.PINNED.value}', '{cls.SOFT_DELETED.value}'"


class SqliteMessageStore(MessageStore):
    """Conversation message history backed by ``memory_session_messages``."""

    def __init__(
        self,
        connection: ConnectionManager,
        scope: RecordScope,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._connection = connection
        self._scope_json = scope.canonical()
        self._ttl_seconds = ttl_seconds

    # -- reads --------------------------------------------------------------

    async def load_messages(self) -> list[dict[str, Any]]:
        rows = await self._connection.query_all(
            f"SELECT message_json, state FROM memory_session_messages "
            f"WHERE scope_key = ? AND state IN ({MessageRowState.active()}) "
            f"ORDER BY seq",
            (self._scope_json,),
        )
        messages: list[dict[str, Any]] = []
        for row in rows:
            message: dict[str, Any] = json.loads(row[0])
            if row[1] == MessageRowState.PINNED:
                message["_pinned"] = True
            messages.append(message)
        return messages

    async def load_all_messages(self) -> list[dict[str, Any]]:
        rows = await self._connection.query_all(
            f"SELECT message_json, state FROM memory_session_messages "
            f"WHERE scope_key = ? "
            f"AND state IN ({MessageRowState.all_visible()}) "
            f"ORDER BY seq",
            (self._scope_json,),
        )
        messages: list[dict[str, Any]] = []
        for row in rows:
            message: dict[str, Any] = json.loads(row[0])
            if row[1] == MessageRowState.PINNED:
                message["_pinned"] = True
            elif row[1] == MessageRowState.SOFT_DELETED:
                message["_deleted"] = True
            messages.append(message)
        return messages

    async def get_revision(self) -> StorageRevision:
        row = await self._connection.query_one(
            "SELECT message_count, version, updated_at FROM memory_revisions WHERE scope_key = ?",
            (self._scope_json,),
        )
        if row is None:
            return StorageRevision(
                message_count=0,
                updated_at=datetime.now(UTC),
                version=0,
            )
        return StorageRevision(
            message_count=int(row[0]),
            updated_at=datetime.fromtimestamp(float(row[2]), tz=UTC),
            version=int(row[1]),
        )

    # -- writes -------------------------------------------------------------

    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        async with self._connection.transaction(immediate=True) as tx:
            await tx.execute(
                "DELETE FROM memory_session_messages WHERE scope_key = ?",
                (self._scope_json,),
            )
            now = time.time()
            for seq, message in enumerate(messages, start=1):
                await tx.execute(
                    "INSERT INTO memory_session_messages "
                    "(scope_key, scope, seq, role, message_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self._scope_json,
                        self._scope_json,
                        seq,
                        str(message.get("role", "user")),
                        json.dumps(message, ensure_ascii=False),
                        now,
                    ),
                )
            await self._bump_revision_tx(tx, len(messages))
        return await self.get_revision()

    async def append_message(self, message: dict[str, Any]) -> StorageRevision:
        async with self._connection.transaction(immediate=True) as tx:
            row = await tx.query_one(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM memory_session_messages WHERE scope_key = ?",
                (self._scope_json,),
            )
            seq = int(row[0]) if row is not None else 1
            await tx.execute(
                "INSERT INTO memory_session_messages "
                "(scope_key, scope, seq, role, message_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._scope_json,
                    self._scope_json,
                    seq,
                    str(message.get("role", "user")),
                    json.dumps(message, ensure_ascii=False),
                    time.time(),
                ),
            )
            await self._bump_revision_tx(tx, count_override=None)
        return await self.get_revision()

    # -- state machine ------------------------------------------------------

    async def prune_messages(self, max_messages: int) -> tuple[int, list[dict[str, Any]]]:
        """Soft-delete oldest non-pinned messages beyond *max_messages*.

        Returns ``(pruned_count, pruned_messages)`` in the same transaction
        as the soft-delete UPDATE.
        """
        async with self._connection.transaction(immediate=True) as tx:
            rows = await tx.query_all(
                f"SELECT seq, message_json, state FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({MessageRowState.active()}) "
                f"ORDER BY seq",
                (self._scope_json,),
            )
            total = len(rows)
            if total <= max_messages:
                return 0, []

            keep_seqs: set[int] = set()
            if max_messages > 0:
                keep_seqs = {int(r[0]) for r in rows[-max_messages:]}
            for row in rows:
                if row[2] == MessageRowState.PINNED:
                    keep_seqs.add(int(row[0]))

            pruned: list[dict[str, Any]] = []
            now = time.time()
            for row in rows:
                seq = int(row[0])
                if seq not in keep_seqs:
                    pruned.append(json.loads(row[1]))
                    await tx.execute(
                        f"UPDATE memory_session_messages "
                        f"SET state = '{MessageRowState.SOFT_DELETED}', deleted_at = ? "
                        f"WHERE scope_key = ? AND seq = ?",
                        (now, self._scope_json, seq),
                    )
            await self._bump_revision_tx(tx, count_override=None)
            return len(pruned), pruned

    async def pin_message(self, message_id: str) -> None:
        await self._connection.execute(
            f"UPDATE memory_session_messages SET state = '{MessageRowState.PINNED}' "
            f"WHERE scope_key = ? AND state = '{MessageRowState.NORMAL}' "
            f"AND (json_extract(message_json, '$.id') = ? "
            f"     OR json_extract(message_json, '$.message_id') = ?)",
            (self._scope_json, message_id, message_id),
        )

    async def unpin_message(self, message_id: str) -> None:
        await self._connection.execute(
            f"UPDATE memory_session_messages SET state = '{MessageRowState.NORMAL}' "
            f"WHERE scope_key = ? AND state = '{MessageRowState.PINNED}' "
            f"AND (json_extract(message_json, '$.id') = ? "
            f"     OR json_extract(message_json, '$.message_id') = ?)",
            (self._scope_json, message_id, message_id),
        )

    async def delete_message(self, message_id: str) -> bool:
        row = await self._connection.query_one(
            "SELECT 1 FROM memory_session_messages WHERE scope_key = ? "
            "AND (json_extract(message_json, '$.id') = ? "
            "     OR json_extract(message_json, '$.message_id') = ?)",
            (self._scope_json, message_id, message_id),
        )
        if row is None:
            return False
        await self._connection.execute(
            "DELETE FROM memory_session_messages WHERE scope_key = ? "
            "AND (json_extract(message_json, '$.id') = ? "
            "     OR json_extract(message_json, '$.message_id') = ?)",
            (self._scope_json, message_id, message_id),
        )
        return True

    async def cleanup_expired(self) -> int:
        cutoff = time.time() - self._ttl_seconds
        async with self._connection.transaction(immediate=True) as tx:
            rows = await tx.query_all(
                f"SELECT COUNT(*) FROM memory_session_messages "
                f"WHERE scope_key = ? AND state = '{MessageRowState.SOFT_DELETED}' "
                f"AND deleted_at < ?",
                (self._scope_json, cutoff),
            )
            count = int(rows[0][0]) if rows else 0
            if count:
                await tx.execute(
                    f"DELETE FROM memory_session_messages "
                    f"WHERE scope_key = ? AND state = '{MessageRowState.SOFT_DELETED}' "
                    f"AND deleted_at < ?",
                    (self._scope_json, cutoff),
                )
        return count

    async def retain_messages(
        self,
        keep_messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        async with self._connection.transaction(immediate=True) as tx:
            if expected_revision is not None:
                row = await tx.query_one(
                    "SELECT message_count, version FROM memory_revisions WHERE scope_key = ?",
                    (self._scope_json,),
                )
                if row is None:
                    if expected_revision.message_count != 0 or expected_revision.version != 0:
                        return None
                else:
                    if int(row[0]) != expected_revision.message_count:
                        return None
                    if int(row[1]) != expected_revision.version:
                        return None

            rows = await tx.query_all(
                f"SELECT seq, message_json FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({MessageRowState.active()}) "
                f"ORDER BY seq",
                (self._scope_json,),
            )

            keep_sigs = {message_signature(m) for m in keep_messages}
            now = time.time()
            soft_deleted = 0
            for row in rows:
                seq = int(row[0])
                stored_msg = json.loads(row[1])
                if message_signature(stored_msg) not in keep_sigs:
                    await tx.execute(
                        f"UPDATE memory_session_messages "
                        f"SET state = '{MessageRowState.SOFT_DELETED}', deleted_at = ? "
                        f"WHERE scope_key = ? AND seq = ?",
                        (now, self._scope_json, seq),
                    )
                    soft_deleted += 1

            if soft_deleted > 0:
                await self._bump_revision_tx(tx, count_override=None)

        return await self.get_revision()

    # -- revision helper ----------------------------------------------------

    async def _bump_revision_tx(
        self,
        tx: Transaction,
        count_override: int | None,
    ) -> None:
        if count_override is not None:
            count = count_override
        else:
            row = await tx.query_one(
                f"SELECT COUNT(*) FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({MessageRowState.active()})",
                (self._scope_json,),
            )
            count = int(row[0]) if row is not None else 0
        await tx.execute(
            "INSERT INTO memory_revisions (scope_key, scope, message_count, version, updated_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(scope_key) DO UPDATE SET "
            "message_count = excluded.message_count, "
            "version = memory_revisions.version + 1, "
            "updated_at = excluded.updated_at",
            (self._scope_json, self._scope_json, count, time.time()),
        )
