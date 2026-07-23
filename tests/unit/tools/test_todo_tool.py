import json
from types import SimpleNamespace

import pytest

from modex_agent.core.agent import current_agent_context
from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import JsonFileTodoStore, TodoItem
from modex_agent.tools.standard.todo_tool import ACTIVE_VIEW_PREFIX


def _active_json(result: str) -> list:
    """Pull the active-view JSON out of a tool result.

    Results now start with a guidance prefix when non-empty; the JSON array
    always begins at the first ``[`` (the prefix contains no ``[``)."""
    return json.loads(result[result.index("["):])


def _set_ctx(session_id: str = "s1") -> object:
    """Set a minimal agent context carrying only the session id (store is on the tool)."""
    ctx = SimpleNamespace(session=SimpleNamespace(session_id=session_id))
    return current_agent_context.set(ctx)


@pytest.mark.asyncio
async def test_write_saves_full_and_returns_active(tmp_path) -> None:
    from modex_agent.tools.standard.todo_tool import TodoWriteTool

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

    parsed = _active_json(result)
    assert [t["content"] for t in parsed] == ["a", "b"]  # active only in return
    assert ACTIVE_VIEW_PREFIX in result  # guidance prefix present for non-empty
    saved = await store.get("s1")
    assert [t.content for t in saved] == ["a", "b"]


@pytest.mark.asyncio
async def test_write_returns_active_only_when_full_includes_completed(tmp_path) -> None:
    """write stores the full list (including completed/cancelled) but only
    returns the active subset (in_progress + pending) for confirmation."""
    from modex_agent.tools.standard.todo_tool import TodoWriteTool

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

    parsed = _active_json(result)
    assert [t["content"] for t in parsed] == ["cur", "next"]  # active only
    saved = await store.get("s1")
    assert [t.content for t in saved] == ["done", "cur", "next", "skipped"]


@pytest.mark.asyncio
async def test_write_rejects_bad_status(tmp_path) -> None:
    from modex_agent.tools.standard.todo_tool import TodoWriteTool

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
    from modex_agent.tools.standard.todo_tool import TodoWriteTool

    # No context set at all
    result = await TodoWriteTool(JsonFileTodoStore(tmp_path)).execute(
        todos=[{"content": "a", "status": "pending"}]
    )
    assert "Error" in result


@pytest.mark.asyncio
async def test_read_returns_active_only_in_order(tmp_path) -> None:
    from modex_agent.tools.standard.todo_tool import TodoReadTool

    store = JsonFileTodoStore(tmp_path)
    await store.save(
        "s1",
        [
            TodoItem(content="done", status=TodoStatus.COMPLETED),
            TodoItem(content="first", status=TodoStatus.PENDING),
            TodoItem(content="cur", status=TodoStatus.IN_PROGRESS),
            TodoItem(content="skipped", status=TodoStatus.CANCELLED),
            TodoItem(content="next", status=TodoStatus.PENDING),
        ],
    )
    token = _set_ctx("s1")
    try:
        result = await TodoReadTool(store).execute()
    finally:
        current_agent_context.reset(token)
    parsed = _active_json(result)
    assert [t["content"] for t in parsed] == ["first", "cur", "next"]
    assert ACTIVE_VIEW_PREFIX in result


def test_tool_names_and_export(tmp_path) -> None:
    from modex_agent.tools.standard import TodoReadTool, TodoWriteTool

    store = JsonFileTodoStore(tmp_path)
    assert TodoWriteTool(store).name == "todo_write"
    assert TodoReadTool(store).name == "todo_read"


@pytest.mark.asyncio
async def test_active_view_text_prefix_only_when_non_empty(tmp_path) -> None:
    """Non-empty active view is prefixed with guidance so the model treats it as
    an ordered work queue; an empty active view returns bare ``"[]"`` (no prefix
    — there is nothing to direct the agent to)."""
    from modex_agent.tools.standard import TodoReadTool, TodoWriteTool

    # Non-empty: prefix present, JSON follows on the next line.
    token = _set_ctx("s1")
    try:
        result = await TodoWriteTool(JsonFileTodoStore(tmp_path)).execute(
            todos=[{"content": "a", "status": "pending"}]
        )
    finally:
        current_agent_context.reset(token)
    assert result.startswith(ACTIVE_VIEW_PREFIX)
    assert _active_json(result) == [{"content": "a", "status": "pending"}]

    # Empty active view (only completed/cancelled): bare "[]", no prefix.
    token = _set_ctx("s1")
    try:
        result = await TodoWriteTool(JsonFileTodoStore(tmp_path)).execute(
            todos=[{"content": "done", "status": "completed"}]
        )
    finally:
        current_agent_context.reset(token)
    assert result == "[]"
    assert ACTIVE_VIEW_PREFIX not in result

    # todo_read on a never-written session: bare "[]", no prefix.
    token = _set_ctx("s-empty")
    try:
        result = await TodoReadTool(JsonFileTodoStore(tmp_path)).execute()
    finally:
        current_agent_context.reset(token)
    assert result == "[]"
