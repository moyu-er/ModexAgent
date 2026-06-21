"""Repro: clicking a session under the HOME workspace shows nothing.

Mirrors the live frontend flow: the browser is on home, so it sends
``ws=<home path>`` on BOTH send_message and fetchMessages. Verifies the home
path resolves to the same dir writes and reads use.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.server import WebUIServer
from framework.workspace.paths import WorkspacePaths
from framework.core.session_id import SessionIdFactory


@pytest.mark.asyncio
async def test_home_path_ws_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        data_dir_name = ".modex"
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(data_dir_name=data_dir_name)
        store.set_agent_pool_map({"main": "main"})
        home_sessions_dir = WorkspacePaths(root=home / data_dir_name).sessions_dir
        server = WebUIServer(
            input_adapter, store, static_dist=None, data_dir=home,
            home_sessions_dir=home_sessions_dir,
        )
        server.set_workspace_index(store)
        server.set_data_dir_name(data_dir_name)
        server.set_agent_pool_map({"main": "main"})
        server.set_pool_agent_names(["main"])
        server.set_session_factory(SessionIdFactory())
        server.set_session_store(
            WorkspacePoolSessionStore(
                base_dir=WorkspacePaths(root=home / data_dir_name).session_index_dir,
                pool_resolver=lambda s: "main",
            )
        )
        from tests.webui._pipeline_fixture import attach_default_pipeline
        attach_default_pipeline(
            server, store, input_adapter, workspace_root=home,
            agent_pool_map={"main": "main"},
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach + send under the HOME path (what the frontend does on home).
            await ws.send_json(
                {"action": "attach", "uuid_prefix": "convH", "pool": "main", "ws": str(home)}
            )
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"
            sid = attached["session_id"]
            await ws.send_json(
                {"action": "send_message", "session_id": sid, "content": "hi home", "ws": str(home)}
            )
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"
            await ws.close()

            # Fetch with ?ws=<home path> (frontend fetchMessages).
            events = await (
                await client.get(f"/api/sessions/{sid}/messages?ws={home}")
            ).json()
            user_msgs = [e for e in events if e.get("event") == "user_message"]
            assert any("home" in e.get("content", "") for e in user_msgs), (
                f"home-path ws message not found; events={events}"
            )
        finally:
            await client.close()
