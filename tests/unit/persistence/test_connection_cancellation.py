from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from typing import Literal, Never

import aiosqlite
import anyio
import pytest

from modex_agent.persistence import ConnectionManager, DatabaseKind


@pytest.mark.parametrize("query", ["query_one", "query_all"])
@pytest.mark.parametrize("in_transaction", [False, True], ids=["manager", "transaction"])
@pytest.mark.parametrize("phase", ["execute", "fetch"])
@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancelled_query_releases_statement_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: Literal["query_one", "query_all"],
    in_transaction: bool,
    phase: Literal["execute", "fetch"],
    cancellation: Literal["asyncio", "anyio"],
) -> None:
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    connection = manager._require_connection()
    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()
    cancelled: list[asyncio.CancelledError] = []
    scopes: list[anyio.CancelScope] = []

    def block_execute(value: int) -> int:
        loop.call_soon_threadsafe(started.set)
        release.wait()
        return value

    async def block_fetch(cursor: aiosqlite.Cursor) -> Never:
        started.set()
        await anyio.sleep_forever()
        raise AssertionError("fetch must be cancelled")

    async def run_query() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            try:
                if in_transaction:
                    async with manager.transaction() as transaction:
                        if query == "query_one":
                            await transaction.query_one(sql)
                        else:
                            await transaction.query_all(sql)
                elif query == "query_one":
                    await manager.query_one(sql)
                else:
                    await manager.query_all(sql)
                pytest.fail("query must propagate cancellation")
            except asyncio.CancelledError as exc:
                # Retain the real cancellation traceback, as task owners do.
                cancelled.append(exc)
                raise

    try:
        await manager.execute("CREATE TABLE items (value INTEGER)")
        await manager.executemany("INSERT INTO items VALUES (?)", [(1,), (2,), (3,)])
        sql = "SELECT value FROM items"
        if phase == "execute":
            await connection.create_function("block_execute", 1, block_execute)
            sql = "SELECT block_execute(value) FROM items"
        else:
            # Pause at the fetch boundary with a real, already-active SQLite cursor.
            monkeypatch.setattr(aiosqlite.Cursor, "fetchone", block_fetch)
            monkeypatch.setattr(aiosqlite.Cursor, "fetchall", block_fetch)

        task = asyncio.create_task(run_query())
        await started.wait()
        if cancellation == "asyncio":
            task.cancel()
        else:
            scopes[0].cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert len(cancelled) == 1

        # No GC or follow-up SQL to incidentally finalize the abandoned statement.
        await manager.close()
    finally:
        release.set()
        # Reap the worker even when the regression makes checkpointing fail.
        await connection.close()
