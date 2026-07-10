import pytest

from modex_agent.runtime.store import TodoItem, TodoStore
from modex_agent.tools.presets import ToolSupplement, get_supplement_tools


class _FakeTodoStore(TodoStore):
    """In-memory stand-in for verifying supplement tool construction."""

    def __init__(self) -> None:
        self._items: dict[str, list[TodoItem]] = {}

    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        self._items[session_id] = list(todos)

    async def get(self, session_id: str) -> list[TodoItem]:
        return list(self._items.get(session_id, []))

    async def delete(self, session_id: str) -> None:
        self._items.pop(session_id, None)


def test_ast_grep_supplement_returns_both_ast_tools() -> None:
    tools = get_supplement_tools([ToolSupplement.AST_GREP])
    names = sorted(t.name for t in tools)
    assert names == ["ast_grep_replace", "ast_grep_search"]


def test_todo_supplement_returns_both_todo_tools() -> None:
    tools = get_supplement_tools([ToolSupplement.TODO], todo_store=_FakeTodoStore())
    names = sorted(t.name for t in tools)
    assert names == ["todo_read", "todo_write"]


def test_todo_supplement_without_store_raises() -> None:
    with pytest.raises(ValueError, match="requires a todo_store"):
        get_supplement_tools([ToolSupplement.TODO])


def test_supplements_dedup_and_empty() -> None:
    assert get_supplement_tools([]) == []
    t1 = get_supplement_tools([ToolSupplement.AST_GREP, ToolSupplement.AST_GREP])
    t2 = get_supplement_tools([ToolSupplement.AST_GREP])
    assert sorted(x.name for x in t1) == sorted(x.name for x in t2)
