from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING, TypeVar

import aiosqlite
import anyio

if TYPE_CHECKING:
    from modex_agent.persistence.migration import DatabaseKind

SqlParameter = str | int | float | bytes | None
SqlParameters = Sequence[SqlParameter]
ValueT = TypeVar("ValueT", str, int, float, bytes)


class ConnectionNotOpenError(RuntimeError):
    """Raised when an operation requires an open database connection."""


class NestedTransactionError(RuntimeError):
    """Raised when one task attempts to nest manager transactions."""


@asynccontextmanager
async def _query_cursor(
    connection: aiosqlite.Connection, sql: str, parameters: SqlParameters
) -> AsyncIterator[aiosqlite.Cursor]:
    # Own the cursor before SQL starts: cancelling aiosqlite does not stop its worker.
    cursor = await connection.cursor()
    try:
        await cursor.execute(sql, parameters)
        yield cursor
    finally:
        with anyio.CancelScope(shield=True):
            await cursor.close()


class Transaction:
    """Restricted SQL operations available while the manager lock is held."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, parameters: SqlParameters = ()) -> None:
        await self._connection.execute(sql, parameters)

    async def executemany(self, sql: str, parameters: Sequence[SqlParameters]) -> None:
        await self._connection.executemany(sql, parameters)

    async def query_all(self, sql: str, parameters: SqlParameters = ()) -> list[Row]:
        async with _query_cursor(self._connection, sql, parameters) as cursor:
            return list(await cursor.fetchall())

    async def query_one(self, sql: str, parameters: SqlParameters = ()) -> Row | None:
        async with _query_cursor(self._connection, sql, parameters) as cursor:
            return await cursor.fetchone()

    async def query_value(
        self,
        sql: str,
        value_type: type[ValueT],
        parameters: SqlParameters = (),
    ) -> ValueT:
        row = await self.query_one(sql, parameters)
        if row is None:
            raise LookupError("query returned no rows")
        return value_type(row[0])


class ConnectionManager:
    """Own one private SQLite connection and serialize all adapter operations."""

    def __init__(self, db_path: Path, database_kind: DatabaseKind) -> None:
        self._db_path = db_path
        self._database_kind = database_kind
        self._connection: aiosqlite.Connection | None = None
        self._operation_lock = anyio.Lock()
        self._transaction_owner: ContextVar[bool] = ContextVar(
            f"connection_transaction_{id(self)}", default=False
        )

    async def open(self) -> None:
        async with self._operation_lock:
            if self._connection is not None:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self._db_path, isolation_level=None)
            connection.row_factory = Row
            self._connection = connection
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA synchronous=NORMAL")
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA busy_timeout=5000")
            await connection.execute("PRAGMA wal_autocheckpoint=1000")

        from modex_agent.persistence.migration import MigrationRunner

        await MigrationRunner(self, self._database_kind).run_pending()

    async def close(self) -> None:
        async with self._operation_lock:
            connection = self._connection
            if connection is None:
                return
            await connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await connection.close()
            self._connection = None

    async def execute(self, sql: str, parameters: SqlParameters = ()) -> None:
        self._reject_transaction_owner()
        async with self._operation_lock:
            await self._require_connection().execute(sql, parameters)

    async def executemany(self, sql: str, parameters: Sequence[SqlParameters]) -> None:
        self._reject_transaction_owner()
        async with self._operation_lock:
            await self._require_connection().executemany(sql, parameters)

    async def query_all(self, sql: str, parameters: SqlParameters = ()) -> list[Row]:
        self._reject_transaction_owner()
        async with (
            self._operation_lock,
            _query_cursor(self._require_connection(), sql, parameters) as cursor,
        ):
            return list(await cursor.fetchall())

    async def query_one(self, sql: str, parameters: SqlParameters = ()) -> Row | None:
        self._reject_transaction_owner()
        async with (
            self._operation_lock,
            _query_cursor(self._require_connection(), sql, parameters) as cursor,
        ):
            return await cursor.fetchone()

    async def query_value(
        self,
        sql: str,
        value_type: type[ValueT],
        parameters: SqlParameters = (),
    ) -> ValueT:
        row = await self.query_one(sql, parameters)
        if row is None:
            raise LookupError("query returned no rows")
        return value_type(row[0])

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False) -> AsyncIterator[Transaction]:
        if self._transaction_owner.get():
            raise NestedTransactionError("nested transactions are not supported")
        async with self._operation_lock:
            connection = self._require_connection()
            token = self._transaction_owner.set(True)
            try:
                await connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield Transaction(connection)
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
            finally:
                self._transaction_owner.reset(token)

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise ConnectionNotOpenError("database connection is not open")
        return self._connection

    def _reject_transaction_owner(self) -> None:
        if self._transaction_owner.get():
            raise NestedTransactionError(
                "use the transaction handle for operations inside a transaction"
            )
