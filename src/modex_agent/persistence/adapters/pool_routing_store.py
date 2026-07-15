"""SQLite-backed :class:`~modex_agent.multi_agent.pool_router.PoolRoutingStore`.

Session-prefix → pool routing is stored in the ``pool_routing`` table. Unlike
:class:`~modex_agent.multi_agent.pool_router.LocalFilePoolRoutingStore`,
``rename_pool`` is a single atomic ``UPDATE`` (no per-file scan/rewrite), and
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

    The ``pool_name`` column and the ``pool`` generated column (extracted from
    ``scope.$.pool``) must agree and be non-empty. A mismatch indicates the
    row was tampered with outside the adapter.
    """


class SqlitePoolRoutingStore(PoolRoutingStore):
    """Session-prefix → pool routing backed by the ``pool_routing`` table."""

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
            "SELECT pool_name, pool FROM pool_routing WHERE session_prefix = ?",
            (session_prefix,),
        ).fetchone()
        if row is None:
            return None
        pool_name: str = row["pool_name"]
        scope_pool: str | None = row["pool"]
        if not pool_name or scope_pool is None or scope_pool != pool_name:
            raise PoolRoutingCorruptionError(
                f"corrupt pool_routing row for prefix {session_prefix!r}: "
                f"pool_name={pool_name!r}, scope_pool={scope_pool!r}"
            )
        return pool_name

    def set_pool(self, session_prefix: str, pool_name: str) -> None:
        scope = json.dumps({"pool": pool_name}, ensure_ascii=False)
        self._conn.execute(
            "INSERT INTO pool_routing (session_prefix, pool_name, scope) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(session_prefix) DO UPDATE SET "
            "pool_name = excluded.pool_name, "
            "scope = excluded.scope, "
            "updated_at = datetime('now')",
            (session_prefix, pool_name, scope),
        )

    def delete_pool(self, session_prefix: str) -> None:
        self._conn.execute(
            "DELETE FROM pool_routing WHERE session_prefix = ?",
            (session_prefix,),
        )

    def rename_pool(self, old_pool: str, new_pool: str) -> int:
        """Atomically rename all routes from *old_pool* to *new_pool*.

        A single ``UPDATE`` rewrites both ``pool_name`` and the ``scope`` JSON
        so the ``pool`` generated column stays consistent. Returns the number
        of rows changed.
        """
        scope = json.dumps({"pool": new_pool}, ensure_ascii=False)
        cursor = self._conn.execute(
            "UPDATE pool_routing "
            "SET pool_name = ?, scope = ?, updated_at = datetime('now') "
            "WHERE pool_name = ?",
            (new_pool, scope, old_pool),
        )
        return cursor.rowcount

    def list_prefixes(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT session_prefix FROM pool_routing ORDER BY session_prefix"
        ).fetchall()
        return [str(row["session_prefix"]) for row in rows]

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
