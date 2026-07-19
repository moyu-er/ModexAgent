"""SQLite-backed :class:`~modex_agent.multi_agent.pool_router.PoolRoutingStore`.

Session-prefix → pool routing is stored in the ``pool_routing`` table. Unlike
:class:`~modex_agent.multi_agent.pool_router.LocalFilePoolRoutingStore`,
routing corruption raises an explicit error instead of silently skipping.

Uses a synchronous ``sqlite3`` connection because the ``PoolRoutingStore`` ABC
is synchronous. The database file must already exist and be migrated — open a
:class:`~modex_agent.persistence.connection.ConnectionManager` first to run
migrations.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from sqlite3 import Row

from modex_agent.multi_agent.pool_router import PoolRoutingStore


class PoolRoutingCorruptionError(RuntimeError):
    """Raised when a ``pool_routing`` row has inconsistent data.

    Retained for backward compatibility with callers that catch this
    exception type. The ADR-0028 schema no longer has a ``pool`` generated
    column, so the adapter does not raise this from ``get_pool``; the class
    is kept exported so downstream code importing it continues to load.
    """


class SqlitePoolRoutingStore(PoolRoutingStore):
    """Session-prefix → pool routing backed by the ``pool_routing`` table.

    The ``scope_key`` column is populated from a minimal
    ``{"pool": <pool_name>}`` JSON (matching the previous ``scope`` column's
    contents) so the cascade cleaner (T11) can still locate rows by scope.
    ``created_at``/``updated_at`` are owned by the schema DEFAULT + the
    ``trg_pool_routing_auto_updated_at`` trigger (ADR-0029), so the adapter
    does not write them explicitly.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit — each statement is its own tx
        )
        self._conn.row_factory = Row
        self._conn.execute("PRAGMA busy_timeout=5000")

    # -- PoolRoutingStore ABC ---------------------------------------------

    def get_pool(self, session_prefix: str) -> str | None:
        row: Row | None = self._conn.execute(
            "SELECT pool_name FROM pool_routing WHERE session_prefix = ?",
            (session_prefix,),
        ).fetchone()
        if row is None:
            return None
        return row["pool_name"]

    def set_pool(self, session_prefix: str, pool_name: str) -> None:
        scope_key = json.dumps({"pool": pool_name}, ensure_ascii=False)
        # updated_at is owned by the trigger (fires on UPDATE when unchanged);
        # created_at/updated_at on INSERT use the schema DEFAULT.
        self._conn.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope_key) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_prefix) DO UPDATE SET "
            "pool_name = excluded.pool_name, "
            "scope_key = excluded.scope_key",
            (session_prefix, pool_name, scope_key),
        )

    def delete_pool(self, session_prefix: str) -> None:
        self._conn.execute(
            "DELETE FROM pool_routing WHERE session_prefix = ?",
            (session_prefix,),
        )

    def list_prefixes(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_prefix FROM pool_routing ORDER BY session_prefix"
        ).fetchall()
        return [str(row["session_prefix"]) for row in rows]

    def delete_pool_routes(self, pool_name: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM pool_routing WHERE pool_name = ?",
            (pool_name,),
        )
        return cursor.rowcount

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
