"""Tests for WebUIServer REST API and WebSocket handler."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.webui.server import WebUIServer
from bot.webui.transcript_store import JSONLTranscriptStore


async def _post_json(
    client: TestClient, path: str, body: dict[str, object] | None
) -> dict[str, object]:
    """Helper: POST JSON to the test client and return parsed response."""
    if body is not None:
        resp = await client.post(path, json=body)
    else:
        resp = await client.post(path)
    assert resp.status == 200
    return await resp.json()


@pytest.mark.asyncio
async def test_api_sessions_list_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = JSONLTranscriptStore(Path(tmp))
        server = WebUIServer(input_adapter, store, static_dist=None)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions")
            assert resp.status == 200
            data = await resp.json()
            assert data == []
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_api_create_conversation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = JSONLTranscriptStore(Path(tmp))
        server = WebUIServer(input_adapter, store, static_dist=None)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.post("/api/sessions")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["conversation_id"]) == 12  # uuid4 hex
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_echoes_user_message() -> None:
    """After send_message, the server MUST echo a user_message event back."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = JSONLTranscriptStore(Path(tmp))
        server = WebUIServer(input_adapter, store, static_dist=None)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach to a conversation
            await ws.send_json({"action": "attach", "conversation_id": "web:test"})
            attached = await ws.receive_json()
            assert attached["event"] == "attached"

            # Send a message
            await ws.send_json({"action": "send_message", "conversation_id": "web:test", "content": "hello"})

            # Should receive user_message echoed back
            echoed = await ws.receive_json(timeout=2)
            assert echoed["event"] == "user_message", f"Expected user_message, got {echoed['event']}"
            assert echoed["conversation_id"] == "web:test"
            assert echoed["content"] == "hello"
            assert echoed["agent_name"] == "main"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_api_messages_loads_transcript() -> None:
    """GET /api/sessions/{conv}/messages returns stored events."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = JSONLTranscriptStore(Path(tmp))
        from bot.webui.events import UserMessageEvent
        store.append("abc123", "main", UserMessageEvent(conversation_id="abc123", agent_name="main", content="hello"))
        server = WebUIServer(input_adapter, store, static_dist=None)
        # Inject the conversation into the server's in-memory set
        server._conversations.add("abc123")
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/abc123/messages")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 1
            assert data[0]["event"] == "user_message"
            assert data[0]["content"] == "hello"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_no_static_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = JSONLTranscriptStore(Path(tmp))
        server = WebUIServer(input_adapter, store, static_dist=None)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/webui/")
            assert resp.status == 503
        finally:
            await client.close()


# ── Pool-per-conversation tests (T3) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_with_pool_persists_mapping() -> None:
    """POST /api/sessions with pool creates conversation in that pool."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_pool_agent_names(["main", "coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        data = await _post_json(
            client, "/api/sessions", {"pool": "coding"}
        )
        conv_id = data["conversation_id"]
        assert data["pool"] == "coding"
        assert server._conversation_pools.get(conv_id) == "coding"

        # Verify JSON persistence
        pool_file = data_dir / "conversation_pools.json"
        assert pool_file.exists()
        saved = json.loads(pool_file.read_text())
        assert saved[conv_id] == "coding"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_session_without_pool_defaults_to_main() -> None:
    """POST /api/sessions without pool defaults to 'main'."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        data = await _post_json(client, "/api/sessions", {})
        assert data["pool"] == "main"
        assert len(server._conversation_pools) == 0

        pool_file = data_dir / "conversation_pools.json"
        assert not pool_file.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_create_session_no_body_defaults_to_main() -> None:
    """POST /api/sessions with empty body defaults to 'main'."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        data = await _post_json(client, "/api/sessions", None)
        assert data["pool"] == "main"
        assert data["conversation_id"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_list_includes_pool() -> None:
    """GET /api/sessions includes pool field — only for non-empty conversations."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        conv1 = await _post_json(
            client, "/api/sessions", {"pool": "coding"}
        )
        conv2 = await _post_json(client, "/api/sessions", {})

        # Add transcript data so conversations are non-empty
        from bot.webui.events import UserMessageEvent
        store.append(conv1["conversation_id"], "coding",
            UserMessageEvent(conversation_id=conv1["conversation_id"], agent_name="coding", content="hi"))
        store.append(conv2["conversation_id"], "main",
            UserMessageEvent(conversation_id=conv2["conversation_id"], agent_name="main", content="hi"))
        server._conversations.add(conv1["conversation_id"])
        server._conversations.add(conv2["conversation_id"])

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_id = {s["conversation_id"]: s for s in sessions}
        assert by_id[conv1["conversation_id"]]["pool"] == "coding"
        assert by_id[conv2["conversation_id"]]["pool"] == "main"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_session_cleans_up_pool_mapping() -> None:
    """DELETE /api/sessions removes the pool mapping."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        data = await _post_json(
            client, "/api/sessions", {"pool": "coding"}
        )
        conv_id = data["conversation_id"]
        assert server._conversation_pools.get(conv_id) == "coding"

        resp = await client.delete(f"/api/sessions/{conv_id}")
        assert resp.status == 200
        assert conv_id not in server._conversation_pools
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_uses_stored_pool() -> None:
    """WebSocket send_message uses pool from stored mapping, not client data."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )

    # Pre-populate pool mapping
    server._conversation_pools["web:test-pool"] = "coding"

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "conversation_id": "web:test-pool"})
        attached = await ws.receive_json()
        assert attached["event"] == "attached"

        # Send message WITHOUT pool field — server reads from stored mapping
        await ws.send_json({
            "action": "send_message",
            "conversation_id": "web:test-pool",
            "content": "hello from coding pool",
        })

        echoed = await ws.receive_json(timeout=2)
        assert echoed["event"] == "user_message"
        assert echoed["agent_name"] == "coding", (
            f"Expected agent_name='coding' from stored pool, "
            f"got {echoed['agent_name']}"
        )
        assert echoed["content"] == "hello from coding pool"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_attach_restores_pool_routing() -> None:
    """WebSocket attach calls pool_switch_callback with stored pool."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_pool_agent_names(["main", "coding"])

    # Pre-populate pool mapping
    server._conversation_pools["web:test-attach"] = "coding"

    # Set a mock callback to verify it is called
    callback = MagicMock()
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "conversation_id": "web:test-attach"})
        attached = await ws.receive_json()
        assert attached["event"] == "attached"

        # Callback should have been invoked with conv_id and pool_name
        callback.assert_called_once_with("web:test-attach", "coding")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_mapping_persistence_across_restart() -> None:
    """Pool mapping survives server restart via JSON file."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = JSONLTranscriptStore(data_dir / "transcripts")

    # First server instance — create a conversation in the coding pool
    server1 = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    client1 = TestClient(TestServer(server1.app))
    await client1.start_server()
    try:
        data = await _post_json(
            client1, "/api/sessions", {"pool": "coding"}
        )
        conv_id = data["conversation_id"]
    finally:
        await client1.close()

    # Verify file exists
    pool_file = data_dir / "conversation_pools.json"
    assert pool_file.exists()

    # Second server instance — should load the mapping from disk
    server2 = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    assert server2._conversation_pools.get(conv_id) == "coding"
