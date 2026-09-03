"""CursorStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`DefaultScopedStorage`.
SQLite: :class:`SqliteCursorStore`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.memory.core.split_stores import CursorStore
from modex_agent.memory.scope import MemoryLayerName
from modex_agent.memory.stores.scoped_file import DefaultScopedStorage
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.cursor_store import SqliteCursorStore


@pytest.fixture(params=["file", "sqlite"])
async def cursor_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[CursorStore]:
    """Parametrized CursorStore — file or sqlite."""
    if request.param == "file":
        yield DefaultScopedStorage(
            tmp_path / "cursor_file",
            layer=MemoryLayerName.SESSION,
        )
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteCursorStore(mgr, scope)
        await mgr.close()


class TestCursorStoreConformance:
    """Same behavior on both backends."""

    async def test_default_cursor_is_zero(self, cursor_store: CursorStore) -> None:
        assert await cursor_store.get_last_cursor() == 0

    async def test_named_cursor_default_is_zero(self, cursor_store: CursorStore) -> None:
        assert await cursor_store.get_last_cursor("replay") == 0

    async def test_set_then_get(self, cursor_store: CursorStore) -> None:
        await cursor_store.set_last_cursor("default", 42)
        assert await cursor_store.get_last_cursor() == 42

    async def test_set_named_cursor(self, cursor_store: CursorStore) -> None:
        await cursor_store.set_last_cursor("replay", 10)
        assert await cursor_store.get_last_cursor("replay") == 10

    async def test_cursors_are_independent(self, cursor_store: CursorStore) -> None:
        await cursor_store.set_last_cursor("default", 1)
        await cursor_store.set_last_cursor("replay", 2)
        assert await cursor_store.get_last_cursor() == 1
        assert await cursor_store.get_last_cursor("replay") == 2

    async def test_overwrite_cursor(self, cursor_store: CursorStore) -> None:
        await cursor_store.set_last_cursor("default", 5)
        await cursor_store.set_last_cursor("default", 99)
        assert await cursor_store.get_last_cursor() == 99
