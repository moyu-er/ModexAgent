"""SQLite-backed :class:`~modex_agent.memory.core.split_stores.MessageStore`.

Conversation message history with a per-row state machine:
``normal → pinned → soft_deleted → DELETE``, plus ``superseded`` — stale
physical copies of retained messages after :meth:`retain_messages`
re-inserts the keep list (invisible to every read path, TTL-purged).

- ``normal`` / ``pinned`` rows are returned by :meth:`load_messages`.
- :meth:`prune_messages` atomically soft-deletes (``state='soft_deleted'``,
  ``updated_at`` auto-bumped by trigger) and returns the pruned content in one transaction.
- :meth:`cleanup_expired` physically deletes soft-deleted rows whose
  ``updated_at`` is older than the configured TTL.

ColumnProjection (ADR-0030) extracts ``message_id`` / ``role`` /
``content``+``is_content_json`` / ``token_count`` into typed columns; the
residual dict is stored in ``message_json``. ``message_id`` lookups
(``pin``/``unpin``/``delete``) use column equality instead of
``json_extract``.

Revision metadata (``message_count``, ``version``, ``updated_at``) is tracked
in ``memory_revisions`` and bumped on every write. All timestamps are int ms
(ADR-0029); the ``scope`` column is gone — only ``scope_key`` remains
(ADR-0031).
"""

from __future__ import annotations

from enum import StrEnum
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from modex_agent.core.message import MessageRole
from modex_agent.memory.core.models import StorageRevision
from modex_agent.memory.core.split_stores import MessageStore, message_signature
from modex_agent.persistence.column_projection import (
    ColumnField,
    ColumnProjection,
    ContentCodec,
)
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager, Transaction

#: Default TTL for soft-deleted messages (7 days).
_DEFAULT_TTL_SECONDS: float = 7 * 24 * 3600


class MessageRowState(StrEnum):
    NORMAL = "normal"
    PINNED = "pinned"
    SOFT_DELETED = "soft_deleted"
    SUPERSEDED = "superseded"

    @classmethod
    def active(cls) -> tuple[str, ...]:
        return (cls.NORMAL.value, cls.PINNED.value)

    @classmethod
    def all_visible(cls) -> tuple[str, ...]:
        """History-view states. ``superseded`` rows are stale physical copies
        of re-inserted retained messages — excluded everywhere to avoid
        duplicates."""
        return (cls.NORMAL.value, cls.PINNED.value, cls.SOFT_DELETED.value)

    @classmethod
    def deleted(cls) -> tuple[str, ...]:
        """States eligible for TTL physical purge."""
        return (cls.SOFT_DELETED.value, cls.SUPERSEDED.value)


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _coerce_created_at_ms(value: object, default: int) -> int:
    """Normalize a ``created_at`` value from a message dict to epoch ms.

    Accepts the ADR-0029 int-ms storage form and the ``ChatMessage.to_dict()``
    string form (``"%Y-%m-%d %H:%M:%S"``, user timezone). Anything else falls
    back to *default* (the current time).
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        from datetime import datetime

        from modex_agent.utils.timezone import get_user_timezone

        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=get_user_timezone()
            )
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return default
    return default


#: Projection splitting the message dict into typed columns + residual JSON.
#: ``message_id`` accepts ``id`` or ``message_id`` (priority: ``id`` first,
#: matching the legacy ``_matches_message_id`` helper). ``content`` uses
#: :class:`ContentCodec` with the ``is_content_json`` companion column.
#: ``token_count`` is nullable — NULL values are dropped on assemble.
_MESSAGE_PROJECTION = ColumnProjection(
    fields=(
        ColumnField(column="message_id", dict_keys=("id", "message_id")),
        ColumnField(column="role", dict_keys=("role",)),
        ColumnField(column="content", dict_keys=("content",), codec=ContentCodec()),
        ColumnField(column="token_count", dict_keys=("token_count",)),
    ),
    json_column="message_json",
)

#: Standard SELECT prefix for projection columns + residual JSON, in the
#: order :func:`_assemble_message` expects. ``created_at`` (int ms, ADR-0029)
#: is appended so the original message-creation time round-trips back into
#: the dict — without it, ``ChatMessage.from_dict`` falls back to the
#: default factory (current time), losing fidelity for downstream consumers
#: (e.g. pruned-content time-range extraction).
_PROJECTION_SELECT = (
    "message_id, role, content, is_content_json, token_count, message_json, created_at"
)

#: Number of columns in :data:`_PROJECTION_SELECT`. Callers that append
#: extra columns (e.g. ``state``) use this to locate them by offset.
_PROJECTION_COLUMN_COUNT = 7

#: Index of ``state`` in queries of form ``SELECT {_PROJECTION_SELECT}, state``.
_STATE_COLUMN_INDEX = _PROJECTION_COLUMN_COUNT


def _assemble_message(row: Row, offset: int = 0) -> dict[str, Any]:
    """Reassemble a message dict from a row's projection columns + residual JSON.

    *offset* is the index of the ``message_id`` column within *row*. The
    helper reads seven consecutive columns (``message_id``, ``role``,
    ``content``, ``is_content_json``, ``token_count``, ``message_json``,
    ``created_at``). NULL-valued columns are dropped so nullable fields
    (``token_count``, ``message_id``, ``content``) are not re-injected as
    ``None``. ``created_at`` (int ms) is passed through verbatim —
    :meth:`ChatMessage._parse_created_at` interprets int values >= 1e12 as
    milliseconds.
    """
    columns: dict[str, Any] = {
        "message_id": row[offset],
        "role": row[offset + 1],
        "content": row[offset + 2],
        "is_content_json": row[offset + 3],
        "token_count": row[offset + 4],
    }
    columns = {k: v for k, v in columns.items() if v is not None}
    msg = _MESSAGE_PROJECTION.assemble(columns, row[offset + 5])
    created_at = row[offset + 6]
    if created_at is not None:
        msg["created_at"] = created_at
    return msg


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
        states = MessageRowState.active()
        rows = await self._connection.query_all(
            f"SELECT {_PROJECTION_SELECT}, state FROM memory_session_messages "
            f"WHERE scope_key = ? AND state IN ({_placeholders(len(states))}) "
            f"ORDER BY seq",
            (self._scope_json, *states),
        )
        messages: list[dict[str, Any]] = []
        for row in rows:
            message = _assemble_message(row)
            if row[_STATE_COLUMN_INDEX] == MessageRowState.PINNED:
                message["_pinned"] = True
            messages.append(message)
        return messages

    async def load_all_messages(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []

        states = MessageRowState.all_visible()
        order_and_limit = "ORDER BY seq" if limit is None else "ORDER BY seq DESC LIMIT ?"
        params: tuple[str | int, ...] = (
            self._scope_json,
            *states,
            str(MessageRole.COMPACT),
        )
        if limit is not None:
            params = (*params, limit)
        rows = await self._connection.query_all(
            f"SELECT {_PROJECTION_SELECT}, state FROM memory_session_messages "
            f"WHERE scope_key = ? "
            f"AND state IN ({_placeholders(len(states))}) "
            f"AND role != ? "
            f"{order_and_limit}",
            params,
        )
        if limit is not None:
            rows.reverse()
        messages: list[dict[str, Any]] = []
        for row in rows:
            message = _assemble_message(row)
            if row[_STATE_COLUMN_INDEX] == MessageRowState.PINNED:
                message["_pinned"] = True
            elif row[_STATE_COLUMN_INDEX] == MessageRowState.SOFT_DELETED:
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
                updated_at=now_ms(),
                version=0,
            )
        return StorageRevision(
            message_count=int(row[0]),
            updated_at=int(row[2]),
            version=int(row[1]),
        )

    # -- writes -------------------------------------------------------------

    async def save_messages(self, messages: list[dict[str, Any]]) -> StorageRevision:
        async with self._connection.transaction(immediate=True) as tx:
            await tx.execute(
                "DELETE FROM memory_session_messages WHERE scope_key = ?",
                (self._scope_json,),
            )
            now = now_ms()
            for seq, message in enumerate(messages, start=1):
                columns, message_json = _MESSAGE_PROJECTION.split(message)
                await tx.execute(
                    "INSERT INTO memory_session_messages "
                    "(scope_key, seq, message_id, role, content, is_content_json, "
                    "token_count, message_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._scope_json,
                        seq,
                        columns.get("message_id"),
                        columns.get("role"),
                        columns.get("content"),
                        columns.get("is_content_json", 0),
                        columns.get("token_count"),
                        message_json,
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
            columns, message_json = _MESSAGE_PROJECTION.split(message)
            await tx.execute(
                "INSERT INTO memory_session_messages "
                "(scope_key, seq, message_id, role, content, is_content_json, "
                "token_count, message_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._scope_json,
                    seq,
                    columns.get("message_id"),
                    columns.get("role"),
                    columns.get("content"),
                    columns.get("is_content_json", 0),
                    columns.get("token_count"),
                    message_json,
                    now_ms(),
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
            active = MessageRowState.active()
            rows = await tx.query_all(
                f"SELECT seq, {_PROJECTION_SELECT}, state FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({_placeholders(len(active))}) "
                f"ORDER BY seq",
                (self._scope_json, *active),
            )
            total = len(rows)
            if total <= max_messages:
                return 0, []

            keep_seqs: set[int] = set()
            if max_messages > 0:
                keep_seqs = {int(r[0]) for r in rows[-max_messages:]}
            for row in rows:
                if row[1 + _STATE_COLUMN_INDEX] == MessageRowState.PINNED:
                    keep_seqs.add(int(row[0]))

            pruned: list[dict[str, Any]] = []
            for row in rows:
                seq = int(row[0])
                if seq not in keep_seqs:
                    pruned.append(_assemble_message(row, offset=1))
                    await tx.execute(
                        "UPDATE memory_session_messages "
                        "SET state = ? "
                        "WHERE scope_key = ? AND seq = ?",
                        (
                            MessageRowState.SOFT_DELETED.value,
                            self._scope_json,
                            seq,
                        ),
                    )
            await self._bump_revision_tx(tx, count_override=None)
            return len(pruned), pruned

    async def pin_message(self, message_id: str) -> None:
        await self._connection.execute(
            "UPDATE memory_session_messages SET state = ? "
            "WHERE scope_key = ? AND state = ? AND message_id = ?",
            (
                MessageRowState.PINNED.value,
                self._scope_json,
                MessageRowState.NORMAL.value,
                message_id,
            ),
        )

    async def unpin_message(self, message_id: str) -> None:
        await self._connection.execute(
            "UPDATE memory_session_messages SET state = ? "
            "WHERE scope_key = ? AND state = ? AND message_id = ?",
            (
                MessageRowState.NORMAL.value,
                self._scope_json,
                MessageRowState.PINNED.value,
                message_id,
            ),
        )

    async def delete_message(self, message_id: str) -> bool:
        row = await self._connection.query_one(
            "SELECT 1 FROM memory_session_messages "
            "WHERE scope_key = ? AND message_id = ?",
            (self._scope_json, message_id),
        )
        if row is None:
            return False
        await self._connection.execute(
            "DELETE FROM memory_session_messages "
            "WHERE scope_key = ? AND message_id = ?",
            (self._scope_json, message_id),
        )
        return True

    async def cleanup_expired(self) -> int:
        cutoff = now_ms() - int(self._ttl_seconds * 1000)
        deleted = MessageRowState.deleted()
        async with self._connection.transaction(immediate=True) as tx:
            rows = await tx.query_all(
                f"SELECT COUNT(*) FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({_placeholders(len(deleted))}) "
                f"AND updated_at < ?",
                (self._scope_json, *deleted, cutoff),
            )
            count = int(rows[0][0]) if rows else 0
            if count:
                await tx.execute(
                    f"DELETE FROM memory_session_messages "
                    f"WHERE scope_key = ? AND state IN ({_placeholders(len(deleted))}) "
                    f"AND updated_at < ?",
                    (self._scope_json, *deleted, cutoff),
                )
        return count

    async def replace_active_messages(
        self,
        messages: list[dict[str, Any]],
        expected_revision: StorageRevision | None = None,
    ) -> StorageRevision | None:
        active = MessageRowState.active()
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
            await tx.execute(
                f"DELETE FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({_placeholders(len(active))})",
                (self._scope_json, *active),
            )
            max_seq_row = await tx.query_one(
                "SELECT COALESCE(MAX(seq), 0) FROM memory_session_messages WHERE scope_key = ?",
                (self._scope_json,),
            )
            next_seq = int(max_seq_row[0]) if max_seq_row is not None else 0
            now = now_ms()
            for message in messages:
                next_seq += 1
                columns, message_json = _MESSAGE_PROJECTION.split(message)
                await tx.execute(
                    "INSERT INTO memory_session_messages "
                    "(scope_key, seq, message_id, role, content, is_content_json, "
                    "token_count, message_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._scope_json,
                        next_seq,
                        columns.get("message_id"),
                        columns.get("role"),
                        columns.get("content"),
                        columns.get("is_content_json", 0),
                        columns.get("token_count"),
                        message_json,
                        now,
                    ),
                )
            await self._bump_revision_tx(tx, len(messages))
        return await self.get_revision()

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

            active = MessageRowState.active()
            rows = await tx.query_all(
                f"SELECT seq, {_PROJECTION_SELECT} FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({_placeholders(len(active))}) "
                f"ORDER BY seq",
                (self._scope_json, *active),
            )

            # Classify existing rows: kept content's stale copies become
            # ``superseded`` (invisible everywhere), the rest ``soft_deleted``
            # (visible in the history view).
            keep_sigs = {message_signature(m) for m in keep_messages}
            for row in rows:
                seq = int(row[0])
                stored_msg = _assemble_message(row, offset=1)
                new_state = (
                    MessageRowState.SUPERSEDED
                    if message_signature(stored_msg) in keep_sigs
                    else MessageRowState.SOFT_DELETED
                )
                await tx.execute(
                    "UPDATE memory_session_messages "
                    "SET state = ? "
                    "WHERE scope_key = ? AND seq = ?",
                    (new_state.value, self._scope_json, seq),
                )

            # Re-insert the keep list with fresh seqs so physical order matches
            # logical order — a new head entry (e.g. a compact summary) lands
            # on top with no read-path adjustment. Per-row fields
            # (message_id / token_count / created_at / pinned) are preserved
            # from the incoming dicts; runtime markers are stripped.
            max_seq_row = await tx.query_one(
                "SELECT COALESCE(MAX(seq), 0) FROM memory_session_messages WHERE scope_key = ?",
                (self._scope_json,),
            )
            next_seq = int(max_seq_row[0]) if max_seq_row is not None else 0
            now = now_ms()
            for message in keep_messages:
                next_seq += 1
                stored = {
                    k: v
                    for k, v in message.items()
                    if k not in ("_pinned", "_deleted")
                }
                created_at = stored.pop("created_at", None)
                columns, message_json = _MESSAGE_PROJECTION.split(stored)
                await tx.execute(
                    "INSERT INTO memory_session_messages "
                    "(scope_key, seq, message_id, role, content, is_content_json, "
                    "token_count, message_json, created_at, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._scope_json,
                        next_seq,
                        columns.get("message_id"),
                        columns.get("role"),
                        columns.get("content"),
                        columns.get("is_content_json", 0),
                        columns.get("token_count"),
                        message_json,
                        _coerce_created_at_ms(created_at, now),
                        (
                            MessageRowState.PINNED.value
                            if message.get("_pinned")
                            else MessageRowState.NORMAL.value
                        ),
                    ),
                )

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
            active = MessageRowState.active()
            row = await tx.query_one(
                f"SELECT COUNT(*) FROM memory_session_messages "
                f"WHERE scope_key = ? AND state IN ({_placeholders(len(active))})",
                (self._scope_json, *active),
            )
            count = int(row[0]) if row is not None else 0
        await tx.execute(
            "INSERT INTO memory_revisions (scope_key, message_count, version, updated_at) "
            "VALUES (?, ?, 1, ?) "
            "ON CONFLICT(scope_key) DO UPDATE SET "
            "message_count = excluded.message_count, "
            "version = memory_revisions.version + 1, "
            "updated_at = excluded.updated_at",
            (self._scope_json, count, now_ms()),
        )
