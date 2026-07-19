"""SQLite-backed :class:`~modex_agent.memory.core.split_stores.KVStore`.

Stores arbitrary structured records keyed by string in ``memory_kv``. The
composite primary key ``(scope_key, key)`` enforces one value per key per
scope. Values are JSON-encoded in ``value_json``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from modex_agent.memory.core.split_stores import KVStore
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager


class SqliteKVStore(KVStore):
    """Scoped key/value records backed by ``memory_kv``."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope_json = scope.canonical()

    async def get(self, key: str) -> Any | None:
        row = await self._connection.query_one(
            "SELECT value_json FROM memory_kv WHERE scope_key = ? AND key = ?",
            (self._scope_json, key),
        )
        if row is None:
            return None
        return json.loads(row[0])

    async def set(self, key: str, value: Any) -> None:
        await self._connection.execute(
            "INSERT INTO memory_kv (scope_key, key, value_json, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(scope_key, key) DO UPDATE SET "
            "value_json = excluded.value_json, updated_at = excluded.updated_at",
            (
                self._scope_json,
                key,
                json.dumps(value, ensure_ascii=False),
                now_ms(),
            ),
        )

    async def delete(self, key: str) -> bool:
        cursor = await self._connection.query_one(
            "SELECT 1 FROM memory_kv WHERE scope_key = ? AND key = ?",
            (self._scope_json, key),
        )
        if cursor is None:
            return False
        await self._connection.execute(
            "DELETE FROM memory_kv WHERE scope_key = ? AND key = ?",
            (self._scope_json, key),
        )
        return True

    async def list_keys(self, prefix: str = "") -> list[str]:
        if prefix:
            rows = await self._connection.query_all(
                "SELECT key FROM memory_kv WHERE scope_key = ? AND key LIKE ? ORDER BY key",
                (self._scope_json, prefix + "%"),
            )
        else:
            rows = await self._connection.query_all(
                "SELECT key FROM memory_kv WHERE scope_key = ? ORDER BY key",
                (self._scope_json,),
            )
        return [row[0] for row in rows]
