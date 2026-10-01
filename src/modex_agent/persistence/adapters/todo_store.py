"""Todo store adapters — SQLite and JSON-file :class:`TodoStore` implementations.

Stores per-session todo lists. :class:`SqliteTodoStore` uses the ``todos``
table: each session gets one row keyed by ``session_id``; the todo items are
serialized as a JSON array in the ``items_json`` column. All methods are
async and go through the ``ConnectionManager``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.turn.todo import TodoItem, TodoStore
from modex_agent.utils.file_io import read_json_robust

if TYPE_CHECKING:
    from modex_agent.core.scope import RecordScope
    from modex_agent.persistence.connection import ConnectionManager


class SqliteTodoStore(TodoStore):
    """SQLite-backed per-session todo list using the ``todos`` table.

    The ``scope_key`` column is populated from the injected ``RecordScope``'s
    canonical JSON. Todo items are serialized as a JSON array of
    ``{"content", "status"}`` dicts in the ``items_json`` column (same format
    as :class:`JsonFileTodoStore`).
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


class JsonFileTodoStore(TodoStore):
    """One JSON file per session: ``<base_dir>/<session_id>.json``.

    ``base_dir`` is injected by the caller (pool-aware in production; a tmp dir
    in tests). Atomic write via tmp + os.replace.

    ``_safe_segment`` only neutralizes characters that are genuinely unsafe on
    common filesystems (``/``, ``\\``, ``:``, ``*``, ``?``, ``"``, ``<``, ``>``,
    ``|``). Session ids in this system are ``{prefix}.{agent}[.{invocation_id}]``,
    so the resulting filename is essentially the session id plus ``.json``.
    """

    _SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _safe_segment(cls, raw: str) -> str:
        return cls._SAFE_RE.sub("_", raw)

    def _path(self, session_id: str) -> Path:
        return self._base_dir / f"{self._safe_segment(session_id)}.json"

    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        payload = [todo.to_dict() for todo in todos]
        target = self._path(session_id)
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, target)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    async def get(self, session_id: str) -> list[TodoItem]:
        data = read_json_robust(self._path(session_id))
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
        path = self._path(session_id)
        if path.exists():
            path.unlink()
