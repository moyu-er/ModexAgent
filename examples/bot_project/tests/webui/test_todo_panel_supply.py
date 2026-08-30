"""Todo panel data-path regression — the capability-supply identity chain.

The todo panel consumes ``GET /api/sessions/{id}/todos`` whose store comes
from ``resolve_runtime_stores`` → the pool's ``materialize_deps``
``capability_supply['todo']`` (T11's wiring). This suite pins the data path
end to end on a todo-capability pool:

- the resolver returns the SAME store instance the todo tools write through
  (``TodoToolFactory`` → ``require_todo_supply`` — the real factory path);
- a ``todo_write`` execution through that tool lands in the panel read
  (identity parity: writes via the tool reflect in panel reads);
- a pool WITHOUT the todo capability exposes no panel data (dark-supply
  death — the resolver yields no store and the endpoint falls back to the
  FILE-mode empty read).

No browser harness exists in this repo (tests/webui are aiohttp route
tests); the visual browser pass belongs to the final verification wave.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from bot.webui.workspace_providers import resolve_runtime_stores

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.plugins.assembly.context import PoolContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.capabilities.todo import TodoSupply
from modex_agent.plugins.defaults.tools import TodoToolFactory, ToolConfig
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.tools.standard.todo_tool import TodoWriteTool
from modex_agent.workspace.paths import WorkspacePaths


def _stack_with_pool(pool: str, supply: TodoSupply | None) -> Any:
    """A fake workspace stack whose materialized resources expose one pool
    whose ``materialize_deps`` carry (or omit) the todo capability supply —
    the production threading shape (Stage 3 → AgentMaterializeDeps →
    AgentPool.materialize_deps)."""
    deps = MagicMock()
    deps.capability_supply = {"todo": supply} if supply is not None else {}
    instance = MagicMock()
    instance.pool.materialize_deps = deps
    resources = MagicMock()
    resources.pools = {pool: instance}
    stack = MagicMock()
    stack.registry.get_or_open = AsyncMock(return_value=MagicMock())
    stack.registry.materialize = AsyncMock(return_value=resources)
    return stack


async def _tool_written_todos(store: JsonFileTodoStore, session_id: str) -> str:
    """Execute a REAL ``todo_write`` (built by the production factory from
    the same supply the panel reads) and return its tool result."""
    supply = TodoSupply(store=store)
    pool_ctx = PoolContext(pool_runtime=PoolRuntimeDeps(capability_supply={"todo": supply}))
    tool = await TodoToolFactory(TodoWriteTool).create(ToolConfig(), pool_ctx)

    from modex_agent.core.session_id import SessionInfo

    agent_ctx = AgentContext(
        system_prompt="",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
    )
    token = current_agent_context.set(agent_ctx)
    try:
        return await tool.execute(
            todos=[
                {"content": "write the section provider", "status": "completed"},
                {"content": "prove panel parity", "status": "in_progress"},
            ]
        )
    finally:
        current_agent_context.reset(token)


def _server(store_resolver: Any, workspace_root: Path) -> WebUIServer:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
    server = WebUIServer(
        WebSocketInputAdapter(), store, static_dist=None, home_sessions_dir=home_sessions_dir
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(".modex")
    server.set_store_resolver(store_resolver)
    return server


class TestTodoPanelSupplyRegression:
    async def test_tool_writes_reflect_in_panel_reads(self, tmp_path: Path) -> None:
        """A todo-capability pool: the panel reads the capability-supply
        store instance, and ``todo_write`` executions through the real
        factory-built tool are visible in the panel read."""
        workspace_root = tmp_path
        store = JsonFileTodoStore(tmp_path / "todos")
        stack = _stack_with_pool("coder", TodoSupply(store=store))
        app_config = AppConfig()
        server = _server(
            lambda ws_root, pool: resolve_runtime_stores(stack, app_config, ws_root, pool),
            workspace_root,
        )
        session_id = "inv42.orchestrator"

        # identity: the resolver surfaces the supply's store instance
        stores = await resolve_runtime_stores(stack, app_config, workspace_root, "coder")
        assert stores.todo_store is store

        result = await _tool_written_todos(store, session_id)
        assert "prove panel parity" in result

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/todos?pool=coder")
            assert resp.status == 200
            data = await resp.json()
            assert data == [{"content": "prove panel parity", "status": "in_progress"}]
        finally:
            await client.close()

    async def test_pool_without_todo_capability_exposes_no_panel_data(self, tmp_path: Path) -> None:
        """A pool whose agents do not carry the todo capability has no
        supply entry: the resolver yields no store and the endpoint falls
        back to the FILE-mode empty read (dark-supply death, SPEC P5)."""
        workspace_root = tmp_path
        stack = _stack_with_pool("default", None)
        app_config = AppConfig()
        server = _server(
            lambda ws_root, pool: resolve_runtime_stores(stack, app_config, ws_root, pool),
            workspace_root,
        )

        stores = await resolve_runtime_stores(stack, app_config, workspace_root, "default")
        assert stores.todo_store is None

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/abc123.default/todos")
            assert resp.status == 200
            assert await resp.json() == []
        finally:
            await client.close()
