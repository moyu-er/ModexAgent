"""SQLite-backed :class:`~modex_agent.memory.core.split_stores.CursorStore`.

Named cursors track how far a consumer has read through an append-only stream.
The composite primary key ``(scope_key, cursor_name)`` enforces one value per
cursor per scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.memory.core.split_stores import CursorStore
from modex_agent.utils.time import now_ms

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager


class SqliteCursorStore(CursorStore):
    """Monotonic processing cursors backed by ``memory_cursors``."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope_json = scope.canonical()

    async def get_last_cursor(self, cursor_name: str = "default") -> int:
        row = await self._connection.query_one(
            "SELECT cursor_value FROM memory_cursors WHERE scope_key = ? AND cursor_name = ?",
            (self._scope_json, cursor_name),
        )
        if row is None:
            return 0
        return int(row[0])

    async def set_last_cursor(self, cursor_name: str, cursor: int) -> None:
        await self._connection.execute(
            "INSERT INTO memory_cursors (scope_key, cursor_name, cursor_value, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(scope_key, cursor_name) DO UPDATE SET "
            "cursor_value = excluded.cursor_value, updated_at = excluded.updated_at",
            (self._scope_json, cursor_name, cursor, now_ms()),
        )
