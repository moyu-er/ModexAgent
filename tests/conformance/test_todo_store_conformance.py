"""TodoStore conformance — same assertions for ``file`` and ``sqlite`` backends.

File: :class:`JsonFileTodoStore`.
SQLite: :class:`SqliteTodoStore`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.todo_store import SqliteTodoStore
from modex_agent.runtime.store import JsonFileTodoStore, TodoItem, TodoStatus, TodoStore


def _items(*contents: str) -> list[TodoItem]:
    return [TodoItem(content=c, status=TodoStatus.PENDING) for c in contents]


@pytest.fixture(params=["file", "sqlite"])
async def todo_store(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    scope: RecordScope,
) -> AsyncGenerator[TodoStore]:
    """Parametrized TodoStore — file or sqlite."""
    if request.param == "file":
        yield JsonFileTodoStore(tmp_path / "todos")
    else:
        mgr = ConnectionManager(tmp_path / "workspace.db", DatabaseKind.WORKSPACE)
        await mgr.open()
        yield SqliteTodoStore(mgr, scope)
        await mgr.close()


class TestTodoStoreConformance:
    """Same behavior on both backends."""

    async def test_get_missing_returns_empty(self, todo_store: TodoStore) -> None:
        assert await todo_store.get("s1") == []

    async def test_save_then_get_roundtrip(self, todo_store: TodoStore) -> None:
        items = _items("task a", "task b")
        await todo_store.save("s1", items)
        result = await todo_store.get("s1")
        assert [t.content for t in result] == ["task a", "task b"]
        assert all(t.status == TodoStatus.PENDING for t in result)

    async def test_save_overwrites_existing(self, todo_store: TodoStore) -> None:
        await todo_store.save("s1", _items("old"))
        await todo_store.save("s1", _items("new1", "new2"))
        result = await todo_store.get("s1")
        assert [t.content for t in result] == ["new1", "new2"]

    async def test_save_empty_list(self, todo_store: TodoStore) -> None:
        await todo_store.save("s1", [])
        assert await todo_store.get("s1") == []

    async def test_delete_removes_todos(self, todo_store: TodoStore) -> None:
        await todo_store.save("s1", _items("task"))
        await todo_store.delete("s1")
        assert await todo_store.get("s1") == []

    async def test_delete_missing_is_noop(self, todo_store: TodoStore) -> None:
        await todo_store.delete("nope")  # must not raise

    async def test_different_sessions_are_isolated(self, todo_store: TodoStore) -> None:
        await todo_store.save("s1", _items("a"))
        await todo_store.save("s2", _items("b"))
        assert [t.content for t in await todo_store.get("s1")] == ["a"]
        assert [t.content for t in await todo_store.get("s2")] == ["b"]
