"""Tests for bot.kb.builder — resolver functions composing the KB stack.

Verifies the assembly contract (DESIGN.md §10):
  build_default_kb_provider composes SqliteKbPersistence + Fts5Retriever
  into a KbProvider, with both backends sharing the same ConnectionManager.

Mirror fixture pattern: test_sqlite_persistence.py (ConnectionManager + migration).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bot.kb.builder import (
    build_default_kb_provider,
    build_kb_provider,
)
from bot.kb.fts5_retriever import Fts5Retriever
from bot.kb.models import KbEntry, KbFilter, KbSearchResult, KbUpsertRequest
from bot.kb.provider import KbProvider
from bot.kb.sqlite_persistence import SqliteKbPersistence
from bot.persistence.migration import BotWorkspaceMigrationRunner

from modex_agent.persistence import ConnectionManager, DatabaseKind


@pytest.fixture
async def connection(tmp_path: Path) -> AsyncIterator[ConnectionManager]:
    conn = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await conn.open()
    await BotWorkspaceMigrationRunner(conn).run_pending()
    yield conn
    await conn.close()


async def test_build_default_kb_provider_returns_kb_provider_instance(
    connection: ConnectionManager,
) -> None:
    """Given: an open migrated workspace connection.
    When: build_default_kb_provider is called.
    Then: the result is a KbProvider instance.
    """
    provider = await build_default_kb_provider(connection)

    assert isinstance(provider, KbProvider)


async def test_default_provider_persistence_is_sqlite(
    connection: ConnectionManager,
) -> None:
    """Given: a provider built by build_default_kb_provider.
    When: inspecting its persistence backend.
    Then: the persistence is a SqliteKbPersistence instance.
    """
    provider = await build_default_kb_provider(connection)

    assert isinstance(provider._persistence, SqliteKbPersistence)


async def test_default_provider_retriever_is_fts5(
    connection: ConnectionManager,
) -> None:
    """Given: a provider built by build_default_kb_provider.
    When: inspecting its retriever backend.
    Then: the retriever is a Fts5Retriever instance.
    """
    provider = await build_default_kb_provider(connection)

    assert isinstance(provider._retriever, Fts5Retriever)


async def test_persistence_and_retriever_share_same_connection(
    connection: ConnectionManager,
) -> None:
    """Given: a provider built by build_default_kb_provider with one connection.
    When: inspecting the connection held by persistence and retriever.
    Then: both hold the exact same ConnectionManager object (identity, not equality).
    """
    provider = await build_default_kb_provider(connection)

    assert isinstance(provider._persistence, SqliteKbPersistence)
    assert isinstance(provider._retriever, Fts5Retriever)
    assert provider._persistence._conn is connection
    assert provider._retriever._conn is connection


async def test_default_provider_preserves_same_key_across_sessions(
    connection: ConnectionManager,
) -> None:
    provider = await build_default_kb_provider(connection)
    await provider.upsert(
        KbUpsertRequest(key="shared", value="one", task_id="task1", session_id="session1")
    )
    await provider.upsert(
        KbUpsertRequest(key="shared", value="two", task_id="task1", session_id="session2")
    )

    first = await provider.get(
        "shared", KbFilter(task_id="task1", session_id="session1")
    )
    second = await provider.get(
        "shared", KbFilter(task_id="task1", session_id="session2")
    )
    assert first is not None and first.value == "one"
    assert second is not None and second.value == "two"


async def test_build_kb_provider_with_mocks_delegates_correctly() -> None:
    """Given: mock persistence and retriever.
    When: build_kb_provider composes them and the provider's upsert and
    search are called.
    Then: the provider delegates upsert to persistence and search to
    retriever with exact arguments, and return values pass through.
    """
    expected_entry = KbEntry(
        entry_id=1, key="k", value="v", created_at=100, updated_at=101,
    )
    expected_results = [KbSearchResult(entry=expected_entry, score=0.9)]
    persistence = AsyncMock()
    persistence.upsert.return_value = expected_entry
    retriever = AsyncMock()
    retriever.search.return_value = expected_results

    provider = build_kb_provider(persistence, retriever)

    assert isinstance(provider, KbProvider)
    request = KbUpsertRequest(key="k", value="v")
    flt = KbFilter(task_id="t1")

    upsert_result = await provider.upsert(request)
    assert upsert_result is expected_entry
    persistence.upsert.assert_awaited_once_with(request)

    search_result = await provider.search("query", flt, limit=5)
    assert search_result is expected_results
    retriever.search.assert_awaited_once_with("query", flt, 5)
