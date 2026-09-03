"""Tests for :class:`SqliteTodoStore` — save/get/delete per-session todos."""

from __future__ import annotations

from modex_agent.core.scope import RecordScope
from modex_agent.persistence import ConnectionManager
from modex_agent.persistence.adapters.todo_store import SqliteTodoStore
from modex_agent.runtime.todo import TodoItem, TodoStatus


def _items(*contents: str) -> list[TodoItem]:
    return [TodoItem(content=c, status=TodoStatus.PENDING) for c in contents]


class TestTodoStoreCRUD:
    async def test_get_missing_returns_empty_list(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        assert await store.get("missing-session") == []

    async def test_save_then_get_roundtrip(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        items = _items("task A", "task B")
        await store.save("s1", items)
        assert await store.get("s1") == items

    async def test_save_overwrites_existing(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        await store.save("s1", _items("old task"))
        await store.save("s1", _items("new task"))
        result = await store.get("s1")
        assert len(result) == 1
        assert result[0].content == "new task"

    async def test_save_empty_list(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        await store.save("s1", [])
        assert await store.get("s1") == []

    async def test_delete_removes_todos(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        await store.save("s1", _items("task A"))
        await store.delete("s1")
        assert await store.get("s1") == []

    async def test_delete_missing_is_noop(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        await store.delete("never-saved")

    async def test_different_sessions_are_isolated(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        await store.save("s1", _items("session-1 task"))
        await store.save("s2", _items("session-2 task"))
        assert await store.get("s1") == _items("session-1 task")
        assert await store.get("s2") == _items("session-2 task")


class TestTodoStoreStatusPreservation:
    async def test_all_statuses_preserved_through_roundtrip(
        self, connection: ConnectionManager, scope: RecordScope
    ) -> None:
        store = SqliteTodoStore(connection, scope)
        items = [
            TodoItem(content="pending task", status=TodoStatus.PENDING),
            TodoItem(content="active task", status=TodoStatus.IN_PROGRESS),
            TodoItem(content="done task", status=TodoStatus.COMPLETED),
            TodoItem(content="cancelled task", status=TodoStatus.CANCELLED),
        ]
        await store.save("s1", items)
        result = await store.get("s1")
        assert result == items
        assert [r.status for r in result] == [
            TodoStatus.PENDING,
            TodoStatus.IN_PROGRESS,
            TodoStatus.COMPLETED,
            TodoStatus.CANCELLED,
        ]
