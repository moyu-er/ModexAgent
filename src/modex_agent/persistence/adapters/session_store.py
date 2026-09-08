"""SQLite-backed :class:`~modex_agent.persistence.session_store.SessionStore`.

Session metadata is stored in the ``sessions`` table. The ``scope_key`` JSON
column holds structured isolation dimensions (agent_id, session_prefix,
parent_session_id, invocation_id); STORED generated columns extract these for
indexed queries. Free-form session metadata is stored as JSON in
``metadata_json``.

All queries use the generated columns and their indexes — never
``json_extract`` at query time. Timestamps are int ms (ADR-0029).
"""

from __future__ import annotations

import json
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.persistence.session_store import SessionStore

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager

#: Columns selected for SessionInfo reconstruction.
_SESSION_COLUMNS = "session_id, agent_id, parent_session_id, metadata_json, created_at, updated_at"

#: Must match the schema DEFAULT expression — COALESCE fallback must equal
#: leaving the column unset so the auto-updated_at trigger sees no change.
_NOW_MS_SQL = "CAST(strftime('%s','now') AS INTEGER) * 1000"


class SqliteSessionStore(SessionStore):
    """Session metadata CRUD backed by the ``sessions`` table."""

    def __init__(self, connection: ConnectionManager) -> None:
        self._connection = connection

    # -- SessionStore ABC --------------------------------------------------

    async def save(self, session: SessionInfo) -> None:
        scope = self._build_scope(session)
        scope_json = scope.canonical()
        metadata_json = (
            json.dumps(session.metadata, ensure_ascii=False) if session.metadata else None
        )
        await self._connection.execute(
            "INSERT INTO sessions "
            "(session_id, scope_key, metadata_json, created_at, updated_at) "
            f"VALUES (?, ?, ?, COALESCE(?, {_NOW_MS_SQL}), "
            f"COALESCE(?, {_NOW_MS_SQL})) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "scope_key = excluded.scope_key, "
            "metadata_json = excluded.metadata_json, "
            "updated_at = excluded.updated_at",
            (session.session_id, scope_json, metadata_json,
             session.created_at, session.updated_at),
        )

    async def get(self, session_id: str) -> SessionInfo | None:
        row = await self._connection.query_one(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        if row is None:
            return None
        return _row_to_session(row)

    async def delete(self, session_id: str) -> None:
        await self._connection.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            (session_id,),
        )

    async def list_sessions(self) -> list[SessionInfo]:
        rows = await self._connection.query_all(
            f"SELECT {_SESSION_COLUMNS} FROM sessions ORDER BY session_id",
        )
        return [_row_to_session(row) for row in rows]

    async def get_children(self, parent_id: str) -> list[SessionInfo]:
        rows = await self._connection.query_all(
            f"SELECT {_SESSION_COLUMNS} FROM sessions "
            "WHERE parent_session_id = ? ORDER BY session_id",
            (parent_id,),
        )
        return [_row_to_session(row) for row in rows]

    # -- SQLite-specific extensions ----------------------------------------

    async def list_by_prefix(self, session_prefix: str) -> list[SessionInfo]:
        """Return sessions whose generated ``session_prefix`` column matches.

        Uses the ``idx_sessions_prefix`` index on the STORED generated column.
        """
        rows = await self._connection.query_all(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE session_prefix = ? ORDER BY session_id",
            (session_prefix,),
        )
        return [_row_to_session(row) for row in rows]

    # -- helpers -----------------------------------------------------------

    def _build_scope(self, session: SessionInfo) -> RecordScope:
        scope = RecordScope(
            session_id=session.session_id,
            session_prefix=session.session_id_prefix,
            agent_id=session.agent_name,
        )
        if session.parent_session_id is not None:
            scope = scope.model_copy(update={"parent_session_id": session.parent_session_id})
        invocation_id = session.metadata.get("invocation_id")
        if invocation_id is not None:
            scope = scope.model_copy(update={"invocation_id": invocation_id})
        return scope


# -- module-level helpers -------------------------------------------------


def _row_to_session(row: Row) -> SessionInfo:
    metadata_text: str | None = row["metadata_json"]
    metadata: dict[str, Any] = json.loads(metadata_text) if metadata_text else {}
    agent_id: str | None = row["agent_id"]
    return SessionInfo(
        session_id=row["session_id"],
        agent_name=agent_id if agent_id is not None else "unknown",
        parent_session_id=row["parent_session_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        metadata=metadata,
    )
