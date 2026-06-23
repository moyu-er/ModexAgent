import json
from types import SimpleNamespace

import pytest

from framework.core.agent import current_agent_context
from framework.core.types import TodoStatus
from framework.runtime.store import JsonFileTodoStore, TodoItem


def _set_ctx(session_id: str = "s1") -> object:
    """Set a minimal agent context carrying only the session id (store is on the tool)."""
    ctx = SimpleNamespace(session=SimpleNamespace(session_id=session_id))
    return current_agent_context.set(ctx)


@pytest.mark.asyncio
async def test_write_saves_full_and_returns_active(tmp_path) -> None:
    from framework.tools.standard.todo_tool import TodoWriteTool

    store = JsonFileTodoStore(tmp_path)
    token = _set_ctx("s1")
    try:
        result = await TodoWriteTool(store).execute(
            todos=[
                {"content": "a", "status": "pending"},
                {"content": "b", "status": "in_progress"},
            ]
        )
    finally:
        current_agent_context.reset(token)

    parsed = json.loads(result)
    assert [t["content"] for t in parsed] == ["a", "b"]  # active only in return
    saved = await store.get("s1")
    assert [t.content for t in saved] == ["a", "b"]


@pytest.mark.asyncio
async def test_write_returns_active_only_when_full_includes_completed(tmp_path) -> None:
    """write stores the full list (including completed/cancelled) but only
    returns the active subset (in_progress + pending) for confirmation."""
    from framework.tools.standard.todo_tool import TodoWriteTool

    store = JsonFileTodoStore(tmp_path)
    token = _set_ctx("s1")
    try:
        result = await TodoWriteTool(store).execute(
            todos=[
                {"content": "done", "status": "completed"},
                {"content": "cur", "status": "in_progress"},
                {"content": "next", "status": "pending"},
                {"content": "skipped", "status": "cancelled"},
            ]
        )
    finally:
        current_agent_context.reset(token)

    parsed = json.loads(result)
    assert [t["content"] for t in parsed] == ["cur", "next"]  # active only
    saved = await store.get("s1")
    assert [t.content for t in saved] == ["done", "cur", "next", "skipped"]


@pytest.mark.asyncio
async def test_write_rejects_bad_status(tmp_path) -> None:
    from framework.tools.standard.todo_tool import TodoWriteTool

    store = JsonFileTodoStore(tmp_path)
    token = _set_ctx("s1")
    try:
        result = await TodoWriteTool(store).execute(
            todos=[{"content": "a", "status": "nope"}]
        )
    finally:
        current_agent_context.reset(token)
    assert "Error" in result
    assert await store.get("s1") == []


@pytest.mark.asyncio
async def test_write_without_context_returns_error(tmp_path) -> None:
    from framework.tools.standard.todo_tool import TodoWriteTool

    # No context set at all
    result = await TodoWriteTool(JsonFileTodoStore(tmp_path)).execute(
        todos=[{"content": "a", "status": "pending"}]
    )
    assert "Error" in result


@pytest.mark.asyncio
async def test_read_returns_active_only_in_order(tmp_path) -> None:
    from framework.tools.standard.todo_tool import TodoReadTool

    store = JsonFileTodoStore(tmp_path)
    await store.save(
        "s1",
        [
            TodoItem("done", TodoStatus.COMPLETED),
            TodoItem("first", TodoStatus.PENDING),
            TodoItem("cur", TodoStatus.IN_PROGRESS),
            TodoItem("skipped", TodoStatus.CANCELLED),
            TodoItem("next", TodoStatus.PENDING),
        ],
    )
    token = _set_ctx("s1")
    try:
        result = await TodoReadTool(store).execute()
    finally:
        current_agent_context.reset(token)
    parsed = json.loads(result)
    assert [t["content"] for t in parsed] == ["first", "cur", "next"]


def test_tool_names_and_export(tmp_path) -> None:
    from framework.tools.standard import TodoReadTool, TodoWriteTool

    store = JsonFileTodoStore(tmp_path)
    assert TodoWriteTool(store).name == "todo_write"
    assert TodoReadTool(store).name == "todo_read"
