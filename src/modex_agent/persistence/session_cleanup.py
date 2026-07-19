"""Transactional SQLite cleanup for records owned by one session."""

from __future__ import annotations

from sqlite3 import Error as SqliteError
from typing import TYPE_CHECKING, Final

import aiosqlite
from pydantic import ValidationError

from modex_agent.core.scope import RecordScope
from modex_agent.core.session_cleanup import (
    MissingSessionScopeError,
    SessionDatabaseCleaner,
    SessionDatabaseCleanupError,
)

if TYPE_CHECKING:
    from modex_agent.persistence.connection import ConnectionManager, Transaction


_SCOPE_DELETES: Final[tuple[str, ...]] = (
    "approval_audit_log",
    "turn_snapshots",
    "todos",
    "external_session_map",
)
_SCOPE_KEY_DELETES: Final[tuple[str, ...]] = (
    "memory_session_messages",
    "memory_kv",
    "memory_cursors",
    "memory_revisions",
    "memory_archive_entries",
    "memory_archive_state",
)
_INBOX_CHILD_TABLES: Final[tuple[str, ...]] = (
    "inbox_messages",
    "inbox_delivered_ids",
)
_SCOPE_DISCOVERY_SQL: Final[str] = " UNION ".join(
    [
        "SELECT scope_key FROM sessions",
        *(f"SELECT scope_key FROM {table}" for table in _SCOPE_DELETES),
        *(f"SELECT scope_key FROM {table}" for table in _SCOPE_KEY_DELETES),
        "SELECT scope_key FROM inbox_topics",
        *(f"SELECT scope_key FROM {table}" for table in _INBOX_CHILD_TABLES),
    ]
)


class SqliteSessionDatabaseCleaner(SessionDatabaseCleaner):
    """Delete one canonical session scope through a borrowed connection."""

    def __init__(self, connection: ConnectionManager) -> None:
        self._connection = connection

    async def list_session_scopes(
        self,
        session_ids: frozenset[str] | None = None,
    ) -> list[RecordScope]:
        try:
            rows = await self._connection.query_all(_SCOPE_DISCOVERY_SQL)
            scopes: dict[str, RecordScope] = {}
            for row in rows:
                persisted_scope_key = str(row[0])
                try:
                    scope = RecordScope.from_canonical(persisted_scope_key)
                except (ValidationError, ValueError):
                    continue
                if scope.session_id is None:
                    continue
                canonical_key = scope.canonical()
                if session_ids is None or scope.session_id in session_ids:
                    scopes[canonical_key] = scope
        except (
            SqliteError,
            aiosqlite.Error,
            ValidationError,
        ) as exc:
            raise SessionDatabaseCleanupError from exc
        return sorted(scopes.values(), key=RecordScope.canonical)

    async def delete_session_rows(self, scope: RecordScope) -> int:
        if scope.session_id is None:
            raise MissingSessionScopeError
        session_id = scope.session_id
        scope_key = scope.canonical()
        deleted = 0
        try:
            async with self._connection.transaction(immediate=True) as transaction:
                deleted += await self._delete(
                    transaction,
                    "DELETE FROM sessions WHERE session_id = ?",
                    session_id,
                )
                for table in _SCOPE_DELETES:
                    deleted += await self._delete(
                        transaction,
                        f"DELETE FROM {table} WHERE scope_key = ?",
                        scope_key,
                    )
                for table in _SCOPE_KEY_DELETES:
                    deleted += await self._delete(
                        transaction,
                        f"DELETE FROM {table} WHERE scope_key = ?",
                        scope_key,
                    )
                deleted += await self._count_inbox_children(transaction, scope_key)
                deleted += await self._delete(
                    transaction,
                    "DELETE FROM inbox_topics WHERE scope_key = ?",
                    scope_key,
                )
        except (SqliteError, aiosqlite.Error) as exc:
            raise SessionDatabaseCleanupError(scope=scope) from exc
        return deleted

    @staticmethod
    async def _delete(transaction: Transaction, sql: str, *params: str) -> int:
        await transaction.execute(sql, params)
        return await transaction.query_value("SELECT changes()", int)

    @staticmethod
    async def _count_inbox_children(
        transaction: Transaction,
        scope_key: str,
    ) -> int:
        deleted = 0
        for table in _INBOX_CHILD_TABLES:
            deleted += await transaction.query_value(
                f"SELECT count(*) FROM {table} WHERE scope_key = ?",
                int,
                (scope_key,),
            )
        return deleted


__all__ = ["SqliteSessionDatabaseCleaner"]
