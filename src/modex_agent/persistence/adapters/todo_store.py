"""SQLite-backed :class:`~modex_agent.runtime.store.TodoStore`.

Stores per-session todo lists in the ``todos`` table. Each session gets one
row keyed by ``session_id``; the todo items are serialized as a JSON array
in the ``items_json`` column. All methods are async and go through the
``ConnectionManager``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from modex_agent.runtime.store import TodoItem, TodoStore

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager


class SqliteTodoStore(TodoStore):
    """SQLite-backed per-session todo list using the ``todos`` table.

    The ``scope_key`` column is populated from the injected ``RecordScope``'s
    canonical JSON. Todo items are serialized as a JSON array of
    ``{"content", "status"}`` dicts in the ``items_json`` column (same format
    as :class:`~modex_agent.runtime.store.JsonFileTodoStore`).
    ``created_at``/``updated_at`` are owned by the schema DEFAULT + the
    ``trg_todos_auto_updated_at`` trigger (ADR-0029), so the adapter does not
    write them explicitly.

    Args:
        connection: The workspace ``ConnectionManager`` shared with other
            adapters.
        scope: A ``RecordScope`` whose canonical JSON populates the
            ``scope_key`` column.
    """

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        self._connection = connection
        self._scope_json = scope.canonical()

    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        """Upsert the todo list for ``session_id``.

        Replaces the entire list on each save (no incremental updates).
        An empty list writes ``[]`` so the row exists and ``get`` returns
        an empty list rather than ``None``.
        """
        items_json = json.dumps(
            [t.to_dict() for t in todos], ensure_ascii=False
        )
        # updated_at is owned by the trigger (fires on UPDATE when unchanged);
        # created_at/updated_at on INSERT use the schema DEFAULT.
        await self._connection.execute(
            "INSERT INTO todos (session_id, scope_key, items_json) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "items_json = excluded.items_json",
            (session_id, self._scope_json, items_json),
        )

    async def get(self, session_id: str) -> list[TodoItem]:
        """Return the todo list for ``session_id``, or ``[]`` if absent."""
        row = await self._connection.query_one(
            "SELECT items_json FROM todos WHERE session_id = ?",
            (session_id,),
        )
        if row is None:
            return []
        data = json.loads(row[0])
        if not isinstance(data, list):
            return []
        items: list[TodoItem] = []
        for entry in data:
            if isinstance(entry, dict):
                try:
                    items.append(TodoItem.from_dict(entry))
                except (KeyError, ValueError):
                    continue
        return items

    async def delete(self, session_id: str) -> None:
        """Remove the todo list for ``session_id``. No-op if absent."""
        await self._connection.execute(
            "DELETE FROM todos WHERE session_id = ?",
            (session_id,),
        )
