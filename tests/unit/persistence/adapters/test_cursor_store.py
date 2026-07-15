"""Tests for :class:`SqliteCursorStore` — get/set + scope isolation."""

from __future__ import annotations

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore


class TestCursorCRUD:
    async def test_get_default_cursor_returns_zero(self, cursor_store: SqliteCursorStore) -> None:
        assert await cursor_store.get_last_cursor() == 0

    async def test_get_named_cursor_returns_zero_when_unset(
        self, cursor_store: SqliteCursorStore
    ) -> None:
        assert await cursor_store.get_last_cursor("archive") == 0

    async def test_set_and_get_default_cursor(self, cursor_store: SqliteCursorStore) -> None:
        await cursor_store.set_last_cursor("default", 42)

        assert await cursor_store.get_last_cursor() == 42

    async def test_set_and_get_named_cursor(self, cursor_store: SqliteCursorStore) -> None:
        await cursor_store.set_last_cursor("archive", 7)

        assert await cursor_store.get_last_cursor("archive") == 7

    async def test_set_overwrites(self, cursor_store: SqliteCursorStore) -> None:
        await cursor_store.set_last_cursor("default", 10)
        await cursor_store.set_last_cursor("default", 20)

        assert await cursor_store.get_last_cursor("default") == 20

    async def test_independent_cursors(self, cursor_store: SqliteCursorStore) -> None:
        await cursor_store.set_last_cursor("default", 1)
        await cursor_store.set_last_cursor("archive", 100)
        await cursor_store.set_last_cursor("replay", 50)

        assert await cursor_store.get_last_cursor("default") == 1
        assert await cursor_store.get_last_cursor("archive") == 100
        assert await cursor_store.get_last_cursor("replay") == 50


class TestCursorScopeIsolation:
    async def test_separate_scopes_are_isolated(
        self, connection: ConnectionManager, scope: RecordScope, other_scope: RecordScope
    ) -> None:
        store_a = SqliteCursorStore(connection, scope)
        store_b = SqliteCursorStore(connection, other_scope)

        await store_a.set_last_cursor("default", 10)
        await store_b.set_last_cursor("default", 20)

        assert await store_a.get_last_cursor("default") == 10
        assert await store_b.get_last_cursor("default") == 20
