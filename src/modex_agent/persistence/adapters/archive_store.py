"""SQLite-backed :class:`~modex_agent.memory.core.split_stores.ArchiveStore`.

Append-only archive log + per-channel partitioned logs backed by
``memory_archive_entries`` (metadata) and ``memory_archive_state`` (generation
state + ``next_archive_id`` counter).

Archive Markdown files (``{archive_id}/{channel}.md``) remain on the filesystem
— this adapter stores **only metadata** in the DB. The ``_log`` sentinel
channel separates general-log entries (``append_log`` / ``read_logs`` /
``save_logs``) from per-channel entries.
"""

from __future__ import annotations

import json
import time
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from modex_agent.memory.core.split_stores import ArchiveStore

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager, Transaction

#: Sentinel channel for general-log entries (``append_log`` family).
_LOG_CHANNEL = "_log"


class SqliteArchiveStore(ArchiveStore):
    """Archive log + channel logs backed by ``memory_archive_entries``."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope_json = scope.canonical()

    # -- general log (3 methods) --------------------------------------------

    async def append_log(self, entry: dict[str, Any]) -> dict[str, Any]:
        async with self._connection.transaction(immediate=True) as tx:
            archive_id = await self._next_archive_id_tx(tx)
            created_at = entry.get("created_at") or time.time()
            summary = entry.get("summary")
            await tx.execute(
                "INSERT INTO memory_archive_entries "
                "(scope_key, scope, archive_id, channel, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._scope_json, self._scope_json, archive_id, _LOG_CHANNEL, summary, created_at),
            )
            await self._set_next_archive_id_tx(tx, archive_id + 1)
            return {
                **entry,
                "archive_id": archive_id,
                "cursor": archive_id,
                "entry_id": entry.get("entry_id") or archive_id,
                "created_at": created_at,
            }

    async def read_logs(self, since_cursor: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        rows = await self._connection.query_all(
            "SELECT archive_id, summary, created_at FROM memory_archive_entries "
            "WHERE scope_key = ? AND channel = ? AND archive_id > ? "
            "ORDER BY archive_id LIMIT ?",
            (self._scope_json, _LOG_CHANNEL, since_cursor, limit),
        )
        return [self._row_to_entry(row, _LOG_CHANNEL) for row in rows]

    async def save_logs(self, entries: list[dict[str, Any]]) -> None:
        async with self._connection.transaction(immediate=True) as tx:
            await tx.execute(
                "DELETE FROM memory_archive_entries WHERE scope_key = ? AND channel = ?",
                (self._scope_json, _LOG_CHANNEL),
            )
            now = time.time()
            for entry in entries:
                archive_id = int(entry.get("archive_id", 0) or 0)
                await tx.execute(
                    "INSERT INTO memory_archive_entries "
                    "(scope_key, scope, archive_id, channel, summary, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self._scope_json,
                        self._scope_json,
                        archive_id,
                        _LOG_CHANNEL,
                        entry.get("summary"),
                        entry.get("created_at") or now,
                    ),
                )

    # -- archive state (2 methods) ------------------------------------------

    async def read_archive_state(self) -> dict[str, Any] | None:
        row = await self._connection.query_one(
            "SELECT state_json, next_archive_id FROM memory_archive_state WHERE scope_key = ?",
            (self._scope_json,),
        )
        if row is None or row[0] is None:
            return None
        state: dict[str, Any] = json.loads(row[0])
        state["next_archive_id"] = int(row[1])
        return state

    async def write_archive_state(self, state: dict[str, Any]) -> None:
        next_id = state.get("next_archive_id")
        state_json = json.dumps(state, ensure_ascii=False)
        if next_id is not None:
            await self._connection.execute(
                "INSERT INTO memory_archive_state "
                "(scope_key, scope, next_archive_id, state_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_key) DO UPDATE SET "
                "next_archive_id = excluded.next_archive_id, "
                "state_json = excluded.state_json, "
                "updated_at = excluded.updated_at",
                (self._scope_json, self._scope_json, int(next_id), state_json, time.time()),
            )
        else:
            await self._connection.execute(
                "INSERT INTO memory_archive_state "
                "(scope_key, scope, next_archive_id, state_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_key) DO UPDATE SET "
                "state_json = excluded.state_json, "
                "updated_at = excluded.updated_at",
                (self._scope_json, self._scope_json, 1, state_json, time.time()),
            )

    # -- channel log (3 methods) --------------------------------------------

    async def append_channel_log(self, channel: str, entry: dict[str, Any]) -> dict[str, Any]:
        async with self._connection.transaction(immediate=True) as tx:
            archive_id = int(entry.get("archive_id", 0) or 0)
            allocate_id = archive_id == 0
            if archive_id == 0:
                archive_id = await self._next_archive_id_tx(tx)
            created_at = entry.get("created_at") or time.time()
            summary = entry.get("summary")
            await tx.execute(
                "INSERT INTO memory_archive_entries "
                "(scope_key, scope, archive_id, channel, summary, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._scope_json, self._scope_json, archive_id, channel, summary, created_at),
            )
            if allocate_id:
                await self._set_next_archive_id_tx(tx, archive_id + 1)
            return {
                **entry,
                "archive_id": archive_id,
                "channel": channel,
                "cursor": archive_id,
                "entry_id": entry.get("entry_id") or archive_id,
                "created_at": created_at,
            }

    async def read_channel_logs(
        self,
        channel: str,
        since_archive_id: int = 0,
        limit: int = 1_000_000,
    ) -> list[dict[str, Any]]:
        rows = await self._connection.query_all(
            "SELECT archive_id, summary, created_at FROM memory_archive_entries "
            "WHERE scope_key = ? AND channel = ? AND archive_id > ? "
            "ORDER BY archive_id LIMIT ?",
            (self._scope_json, channel, since_archive_id, limit),
        )
        return [self._row_to_entry(row, channel) for row in rows]

    async def save_channel_logs(self, channel: str, entries: list[dict[str, Any]]) -> None:
        async with self._connection.transaction(immediate=True) as tx:
            await tx.execute(
                "DELETE FROM memory_archive_entries WHERE scope_key = ? AND channel = ?",
                (self._scope_json, channel),
            )
            now = time.time()
            for entry in entries:
                archive_id = int(entry.get("archive_id", 0) or 0)
                await tx.execute(
                    "INSERT INTO memory_archive_entries "
                    "(scope_key, scope, archive_id, channel, summary, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self._scope_json,
                        self._scope_json,
                        archive_id,
                        channel,
                        entry.get("summary"),
                        entry.get("created_at") or now,
                    ),
                )

    # -- retention (2 methods) ----------------------------------------------

    async def prune_to_max(self, max_entries: int) -> int:
        """Delete oldest archive entries until distinct archive_id count <= max.

        Returns the number of rows deleted.
        """
        async with self._connection.transaction(immediate=True) as tx:
            id_rows = await tx.query_all(
                "SELECT DISTINCT archive_id FROM memory_archive_entries "
                "WHERE scope_key = ? ORDER BY archive_id",
                (self._scope_json,),
            )
            distinct_ids = [int(r[0]) for r in id_rows]
            if len(distinct_ids) <= max_entries:
                return 0
            to_delete = distinct_ids[: len(distinct_ids) - max_entries]
            if not to_delete:
                return 0
            placeholders = ",".join("?" for _ in to_delete)
            count_row = await tx.query_one(
                f"SELECT COUNT(*) FROM memory_archive_entries "
                f"WHERE scope_key = ? AND archive_id IN ({placeholders})",
                (self._scope_json, *to_delete),
            )
            deleted = int(count_row[0]) if count_row is not None else 0
            await tx.execute(
                f"DELETE FROM memory_archive_entries "
                f"WHERE scope_key = ? AND archive_id IN ({placeholders})",
                (self._scope_json, *to_delete),
            )
        return deleted

    async def cleanup_empty_dirs(self) -> int:
        """No-op — Markdown directories are managed by the archive pipeline."""
        return 0

    # -- helpers ------------------------------------------------------------

    async def _next_archive_id_tx(self, tx: Transaction) -> int:
        row = await tx.query_one(
            "SELECT next_archive_id FROM memory_archive_state WHERE scope_key = ?",
            (self._scope_json,),
        )
        return int(row[0]) if row is not None else 1

    async def _set_next_archive_id_tx(self, tx: Transaction, value: int) -> None:
        await tx.execute(
            "INSERT INTO memory_archive_state "
            "(scope_key, scope, next_archive_id, state_json, updated_at) "
            "VALUES (?, ?, ?, NULL, ?) "
            "ON CONFLICT(scope_key) DO UPDATE SET "
            "next_archive_id = excluded.next_archive_id, "
            "updated_at = excluded.updated_at",
            (self._scope_json, self._scope_json, value, time.time()),
        )

    @staticmethod
    def _row_to_entry(row: Row, channel: str) -> dict[str, Any]:
        archive_id = int(row[0])
        return {
            "archive_id": archive_id,
            "channel": channel,
            "summary": row[1],
            "created_at": float(row[2]),
            "cursor": archive_id,
            "entry_id": archive_id,
        }
