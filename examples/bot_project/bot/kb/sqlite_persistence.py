from __future__ import annotations

from bot.kb.models import KbEntry, KbFilter, KbUpsertRequest
from bot.kb.persistence import KbPersistence
from bot.kb.sqlite_utils import build_filter_clauses
from modex_agent.persistence.connection import ConnectionManager
from modex_agent.utils.time import now_ms
from modex_graph.id_generator import default_id_generator


class SqliteKbPersistence(KbPersistence):
    def __init__(self, connection: ConnectionManager) -> None:
        self._conn = connection

    async def upsert(self, request: KbUpsertRequest) -> KbEntry:
        timestamp = now_ms()
        entry_id = default_id_generator().generate()

        async with self._conn.transaction(immediate=True) as transaction:
            await transaction.execute(
                """
                INSERT INTO kb_entries
                    (entry_id, key, value, task_id, session_id, category, tags,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, session_id, key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    tags = excluded.tags,
                    updated_at = excluded.updated_at
                """,
                (
                    entry_id,
                    request.key,
                    request.value,
                    request.task_id,
                    request.session_id,
                    request.category,
                    request.tags,
                    timestamp,
                    timestamp,
                ),
            )
            row = await transaction.query_one(
                "SELECT entry_id, key, value, task_id, session_id, "
                "category, tags, created_at, updated_at "
                "FROM kb_entries "
                "WHERE task_id = ? AND session_id = ? AND key = ?",
                (request.task_id, request.session_id, request.key),
            )

        if row is None:
            raise RuntimeError("upsert succeeded but row not found")
        return KbEntry(**dict(row))

    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:
        clauses, params = build_filter_clauses(filter)
        clauses.insert(0, "key = ?")
        params.insert(0, key)
        sql = (
            "SELECT entry_id, key, value, task_id, session_id, "
            "category, tags, created_at, updated_at "
            f"FROM kb_entries WHERE {' AND '.join(clauses)} LIMIT 1"
        )

        row = await self._conn.query_one(sql, tuple(params))
        if row is None:
            return None
        return KbEntry(**dict(row))

    async def delete(self, key: str, filter: KbFilter) -> bool:
        clauses, params = build_filter_clauses(filter)
        clauses.insert(0, "key = ?")
        params.insert(0, key)
        sql = f"DELETE FROM kb_entries WHERE {' AND '.join(clauses)}"

        async with self._conn.transaction(immediate=True) as transaction:
            await transaction.execute(sql, tuple(params))
            row = await transaction.query_one("SELECT changes()")
        return row is not None and int(row[0]) > 0

    async def list_keys(
        self,
        filter: KbFilter,
        prefix: str | None = None,
    ) -> list[str]:
        clauses, params = build_filter_clauses(filter)
        if prefix is not None:
            escaped = (
                prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append("key LIKE ? ESCAPE '\\'")
            params.append(f"{escaped}%")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._conn.query_all(
            f"SELECT key FROM kb_entries {where_sql} ORDER BY key",
            tuple(params),
        )
        return [str(row[0]) for row in rows]
