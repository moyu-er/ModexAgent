"""Tests for the todo REST endpoint."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from framework.core.types import TodoStatus
from framework.runtime.store import JsonFileTodoStore, TodoItem
from framework.workspace.paths import WorkspacePaths


@pytest.mark.asyncio
async def test_get_todos_returns_active_items_only() -> None:
    """GET /api/sessions/:id/todos returns pending + in_progress, excluding completed/cancelled."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        server.set_data_dir_name(".modex")
        server.set_agent_pool_map({"main": "main"})

        session_id = "abc123.main"
        todo_dir = workspace_root / ".modex" / "runtime_state" / "main" / "todos"
        todo_store = JsonFileTodoStore(todo_dir)
        await todo_store.save(
            session_id,
            [
                TodoItem("done", TodoStatus.COMPLETED),
                TodoItem("current", TodoStatus.IN_PROGRESS),
                TodoItem("next", TodoStatus.PENDING),
                TodoItem("dropped", TodoStatus.CANCELLED),
            ],
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/todos")
            assert resp.status == 200
            data = await resp.json()
            assert data == [
                {"content": "current", "status": "in_progress"},
                {"content": "next", "status": "pending"},
            ]
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_get_todos_empty_when_no_store() -> None:
    """No todo file for the session -> return empty list (not error)."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        server.set_data_dir_name(".modex")
        server.set_agent_pool_map({"main": "main"})

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/abc123.main/todos")
            assert resp.status == 200
            data = await resp.json()
            assert data == []
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_get_todos_excludes_completed_and_cancelled() -> None:
    """Only pending + in_progress are returned; completed/cancelled never appear."""
    with tempfile.TemporaryDirectory() as tmp:
        workspace_root = Path(tmp)
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
        home_sessions_dir = WorkspacePaths(root=workspace_root / ".modex").sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, home_sessions_dir=home_sessions_dir
        )
        server.set_workspace_index(store)
        server.set_data_dir_name(".modex")
        server.set_agent_pool_map({"main": "main"})

        session_id = "abc123.main"
        todo_dir = workspace_root / ".modex" / "runtime_state" / "main" / "todos"
        todo_store = JsonFileTodoStore(todo_dir)
        await todo_store.save(
            session_id,
            [
                TodoItem("all done", TodoStatus.COMPLETED),
                TodoItem("skipped", TodoStatus.CANCELLED),
            ],
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get(f"/api/sessions/{session_id}/todos")
            assert resp.status == 200
            data = await resp.json()
            assert data == []
        finally:
            await client.close()
