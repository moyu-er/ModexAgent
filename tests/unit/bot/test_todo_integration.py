import json
from types import SimpleNamespace

import pytest

from framework.core.agent import current_agent_context
from framework.runtime.store import JsonFileTodoStore


def _set_ctx(session_id: str) -> object:
    return current_agent_context.set(
        SimpleNamespace(session=SimpleNamespace(session_id=session_id))
    )


@pytest.mark.asyncio
async def test_write_persists_across_store_instances(tmp_path) -> None:
    """A NEW store instance over the same base_dir sees the persisted list
    (simulates cross-turn / cross-restart). The tool returns only the active
    subset; the store keeps the full list including completed items."""
    from framework.tools.standard.todo_tool import TodoReadTool, TodoWriteTool

    token = _set_ctx("s1")
    try:
        result = await TodoWriteTool(JsonFileTodoStore(tmp_path)).execute(
            todos=[
                {"content": "do A", "status": "completed"},
                {"content": "do B", "status": "in_progress"},
                {"content": "do C", "status": "pending"},
            ]
        )
    finally:
        current_agent_context.reset(token)
    assert [t["content"] for t in json.loads(result)] == ["do B", "do C"]

    # fresh store instance (new turn / restart) — read filter keeps active only
    token = _set_ctx("s1")
    try:
        result = await TodoReadTool(JsonFileTodoStore(tmp_path)).execute()
    finally:
        current_agent_context.reset(token)
    active = json.loads(result)
    assert [t["content"] for t in active] == ["do B", "do C"]
    assert all(t["status"] in ("pending", "in_progress") for t in active)


@pytest.mark.asyncio
async def test_session_isolation_through_tool(tmp_path) -> None:
    from framework.tools.standard.todo_tool import TodoWriteTool

    for sid, contents in (("s1", ["a"]), ("s2", ["b", "c"])):
        token = _set_ctx(sid)
        try:
            await TodoWriteTool(JsonFileTodoStore(tmp_path)).execute(
                todos=[{"content": c, "status": "pending"} for c in contents]
            )
        finally:
            current_agent_context.reset(token)

    store = JsonFileTodoStore(tmp_path)
    assert [t.content for t in await store.get("s1")] == ["a"]
    assert [t.content for t in await store.get("s2")] == ["b", "c"]
