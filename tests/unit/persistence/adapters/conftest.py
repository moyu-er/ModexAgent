"""Shared fixtures for SQLite memory adapter tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.archive_store import SqliteArchiveStore
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore
from modex_agent.persistence.adapters.kv_store import SqliteKVStore
from modex_agent.persistence.adapters.message_store import SqliteMessageStore


def _scope(
    *,
    pool: str = "default",
    session_id: str = "s1",
    agent_id: str = "main",
    user_id: str | None = None,
) -> RecordScope:
    """Build a RecordScope for test adapters."""
    return RecordScope(
        pool=pool,
        session_id=session_id,
        agent_id=agent_id,
        user_id=user_id,
    )


@pytest.fixture
async def connection(tmp_path: Path) -> AsyncGenerator[ConnectionManager]:
    """Yield an opened workspace ConnectionManager, closing after the test."""
    mgr = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await mgr.open()
    yield mgr
    await mgr.close()


@pytest.fixture
def scope() -> RecordScope:
    return _scope()


@pytest.fixture
def other_scope() -> RecordScope:
    """A different scope to verify isolation."""
    return _scope(session_id="s2", agent_id="other")


@pytest.fixture
def message_store(connection: ConnectionManager, scope: RecordScope) -> SqliteMessageStore:
    return SqliteMessageStore(connection, scope, ttl_seconds=0.0)


@pytest.fixture
def kv_store(connection: ConnectionManager, scope: RecordScope) -> SqliteKVStore:
    return SqliteKVStore(connection, scope)


@pytest.fixture
def cursor_store(connection: ConnectionManager, scope: RecordScope) -> SqliteCursorStore:
    return SqliteCursorStore(connection, scope)


@pytest.fixture
def archive_store(connection: ConnectionManager, scope: RecordScope) -> SqliteArchiveStore:
    return SqliteArchiveStore(connection, scope)


def msg(mid: str, content: str = "x", **extra: object) -> dict[str, object]:
    """Build a minimal message dict with an id."""
    result: dict[str, Any] = {"id": mid, "role": "user", "content": content}
    result.update(extra)
    return result
