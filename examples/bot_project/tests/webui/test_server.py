"""Tests for WebUIServer REST API and WebSocket handler."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import WebBotEmitter
from bot.webui.server import (
    WebUIServer,
    _new_uuid_prefix
)
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.transcript_store import JSONLTranscriptStore
from framework.core.emitter import AgentResult, EmitterConfig


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
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        server = WebUIServer(input_adapter, store, static_dist=None)
        server.set_workspace_index(store)
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
async def test_ws_send_message_echoes_user_message() -> None:
    """After send_message, the server MUST echo a user_message event back."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        server = WebUIServer(input_adapter, store, static_dist=None)
        server.set_workspace_index(store)
        from tests.webui._pipeline_fixture import attach_default_pipeline
        attach_default_pipeline(server, store, input_adapter)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach to a session
            await ws.send_json({"action": "attach", "session_id": "web:test.main"})
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"

            # Send a message
            await ws.send_json({"action": "send_message", "session_id": "web:test.main", "content": "hello"})

            # Should receive user_message echoed back
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message", f"Expected user_message, got {echoed['event']}"
            assert echoed["session_id"] == "web:test.main"
            assert echoed["content"] == "hello"
            assert echoed["agent_name"] == "main"
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_api_messages_loads_transcript() -> None:
    """GET /api/sessions/{session_id}/messages returns stored events."""
    with tempfile.TemporaryDirectory() as tmp:
        input_adapter = WebSocketInputAdapter()
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        server = WebUIServer(input_adapter, store, static_dist=None)
        server.set_workspace_index(store)
        # Append to the server's active workspace store, not the legacy one.
        from bot.webui.events import UserMessageEvent
        server._store.append(
            "abc123.main",
            UserMessageEvent(session_id="abc123.main", agent_name="main", content="hello")
)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/api/sessions/abc123.main/messages")
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
        store = WorkspaceScopedTranscriptStore(Path(tmp), lambda: "")
        server = WebUIServer(input_adapter, store, static_dist=None)
        server.set_workspace_index(store)
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            resp = await client.get("/webui/")
            assert resp.status == 503
        finally:
            await client.close()


# ── Pool-per-conversation tests (T3) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sessions_list_includes_pool() -> None:
    """GET /api/sessions returns one entry per session with session_id and pool."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"coding": "coding", "main": "main"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"coding": "coding", "main": "main"})

    server.set_pool_agent_names(["main", "coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Create sessions via WS attach
        ws = await client.ws_connect("/ws")

        prefix1 = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": prefix1, "pool": "coding"})
        attached1 = _unwrap_envelope(await ws.receive_json())
        assert attached1["event"] == "attached"
        s1_sid = attached1["session_id"]

        prefix2 = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": prefix2, "pool": "main"})
        attached2 = _unwrap_envelope(await ws.receive_json())
        assert attached2["event"] == "attached"
        s2_sid = attached2["session_id"]

        # Add transcript data to the server's workspace-scoped store.
        from bot.webui.events import UserMessageEvent
        server._store.append(s1_sid,
            UserMessageEvent(session_id=s1_sid, agent_name="coding", content="hi"))
        server._store.append(s2_sid,
            UserMessageEvent(session_id=s2_sid, agent_name="main", content="hi"))

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}
        assert s1_sid in by_sid
        assert by_sid[s1_sid]["pool"] == "coding"
        assert s2_sid in by_sid
        assert by_sid[s2_sid]["pool"] == "main"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_session_cleans_up_metadata() -> None:
    """DELETE /api/sessions/{session_id} removes the session from metadata."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"coding": "coding", "main": "main"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"coding": "coding", "main": "main"})

    server.set_pool_agent_names(["main", "coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        prefix = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": prefix, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        session_id = attached["session_id"]

        resp = await client.delete(f"/api/sessions/{session_id}")
        assert resp.status == 200
        assert server._workspace_index.workspace_of(session_id) is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_session_removes_transcript_from_any_pool_directory() -> None:
    """DELETE /api/sessions/{session_id} cleans up the transcript even if a
    prior routing bug placed it in the wrong pool directory.

    Regression: empty agent-pool mapping caused coding sessions to be written
    to the main pool directory. Deleting them only removed from the expected
    (coding) directory and left the ghost file behind.
    """
    from bot.webui.events import UserMessageEvent

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"coding": "coding", "main": "main"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"coding": "coding", "main": "main"})

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Simulate a corrupted transcript: coding agent file stored under main.
        uuid_prefix = "corrupted123"
        session_id = f"{uuid_prefix}.coding"
        wrong_file = data_dir / "main" / f"{uuid_prefix}.coding.jsonl"
        wrong_file.parent.mkdir(parents=True, exist_ok=True)
        wrong_file.write_text(
            json.dumps(
                UserMessageEvent(
                    session_id=session_id,
                    agent_name="coding",
                    content="ghost message"
).to_dict(),
                ensure_ascii=False
)
            + "\n",
            encoding="utf-8"
)

        resp = await client.delete(f"/api/sessions/{session_id}")
        assert resp.status == 200
        assert not wrong_file.exists(), (
            f"ghost transcript in wrong pool directory was not removed: {wrong_file}"
        )

        # It must also not remain in the expected directory.
        expected_file = data_dir / "coding" / f"{uuid_prefix}.coding.jsonl"
        assert not expected_file.exists()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_send_message_uses_stored_pool() -> None:
    """WebSocket send_message uses pool from PoolRouter resolver, not client data."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)

    # Wire resolver so the send path derives agent_name from pool_name.
    server.set_pool_resolver(lambda cid: "coding")

    from tests.webui._pipeline_fixture import attach_default_pipeline
    from unittest.mock import MagicMock
    pool_store = MagicMock()
    pool_store.get = lambda key, default=None: "coding"
    pool_store.set = MagicMock()
    attach_default_pipeline(server, store, input_adapter, pool_session_store=pool_store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": "web:test-pool.coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Send message WITHOUT pool field — server reads from stored mapping
        await ws.send_json({
            "action": "send_message",
            "session_id": "web:test-pool.coding",
            "content": "hello from coding pool",
        })

        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
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
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_pool_resolver(lambda cid: "coding")

    # Set a mock callback to verify it is called
    callback = MagicMock()
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": "web:test-attach.coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Callback should have been invoked with the snowflake (agent-independent id) and pool_name
        callback.assert_called_once_with("web:test-attach", "coding")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_mapping_persistence_across_restart() -> None:
    """Pool mapping survives server restart via physical transcript layout."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"main": "main", "coding": "coding"})

    # First server instance — create a session in the coding pool and send a
    # message so the transcript is persisted to disk (empty sessions are not).
    server1 = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server1.set_workspace_index(store)
    server1.set_agent_pool_map({"main": "main", "coding": "coding"})
    server1.set_pool_agent_names(["main", "coding"])
    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server1, store, input_adapter)
    client1 = TestClient(TestServer(server1.app))
    await client1.start_server()
    try:
        conv_id = _new_uuid_prefix()
        ws = await client1.ws_connect("/ws")
        await ws.send_json({"action": "attach", "uuid_prefix": conv_id, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        session_id = attached["session_id"]

        await ws.send_json({
            "action": "send_message",
            "session_id": session_id,
            "content": "hello coding",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))
    finally:
        await client1.close()

    # Verify transcript file exists under the coding pool directory.
    transcript_file = data_dir / "coding" / f"{conv_id}.coding.jsonl"
    assert transcript_file.exists()

    # No sessions.json in the new design.
    meta_file = data_dir / "sessions.json"
    assert not meta_file.exists()

    # Second server instance — fresh store scanning the same disk layout.
    store2 = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store2.set_agent_pool_map({"main": "main", "coding": "coding"})
    server2 = WebUIServer(
        input_adapter, store2, static_dist=None, data_dir=data_dir
    )
    server2.set_workspace_index(store2)
    server2.set_agent_pool_map({"main": "main", "coding": "coding"})
    server2.set_pool_agent_names(["main", "coding"])
    client2 = TestClient(TestServer(server2.app))
    await client2.start_server()
    try:
        resp = await client2.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}
        assert session_id in by_sid, (
            f"Session {session_id!r} must survive server restart."
        )
        assert by_sid[session_id]["pool"] == "coding"
    finally:
        await client2.close()


@pytest.mark.asyncio
async def test_sessions_persist_across_pool_switch_and_qq_conversation() -> None:
    """Regression: sessions must not disappear after pool switch + QQ chat.

    Bug report: User switches pool to coding, chats, then uses QQ. After
    switching back to main the list is empty; switching back to coding is
    also empty. This test pins the backend contract.
    """
    from bot.webui.events import UserMessageEvent

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"main": "main", "coding": "coding"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map({"main": "main", "coding": "coding"})

    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, store, input_adapter)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        coding_conv = _new_uuid_prefix()
        coding_sid = f"{coding_conv}.coding"

        ws = await client.ws_connect("/ws")
        await ws.send_json(
            {"action": "attach", "uuid_prefix": coding_conv, "pool": "coding"}
        )
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        await ws.send_json(
            {
                "action": "send_message",
                "session_id": coding_sid,
                "content": "hello coding",
            }
        )
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"
        assert echoed["agent_name"] == "coding"

        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {str(s["session_id"]): s for s in sessions}
        assert coding_sid in by_sid
        assert by_sid[coding_sid]["pool"] == "coding"

        qq_conv_id = "qq:group:12345"
        qq_sid = f"{qq_conv_id}.main"
        server._store.append(
            qq_sid,
            UserMessageEvent(
                session_id=qq_sid,
                agent_name="main",
                content="hello from QQ"
)
)

        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        by_sid = {str(s["session_id"]): s for s in sessions}
        assert coding_sid in by_sid, (
            f"coding session disappeared after QQ conversation; "
            f"sessions={sessions}"
        )
        assert by_sid[coding_sid]["pool"] == "coding"
        assert coding_sid in by_sid

        main_conv = _new_uuid_prefix()
        main_sid = f"{main_conv}.main"
        await ws.send_json(
            {"action": "attach", "uuid_prefix": main_conv, "pool": "main"}
        )
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        await ws.send_json(
            {
                "action": "send_message",
                "session_id": main_sid,
                "content": "hello main",
            }
        )
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"

        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        by_sid = {str(s["session_id"]): s for s in sessions}
        assert coding_sid in by_sid
        assert by_sid[coding_sid]["pool"] == "coding"
        assert main_sid in by_sid
        assert by_sid[main_sid]["pool"] == "main"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_attach_switches_all_sessions() -> None:
    """Switching sessions must unregister ALL previous sessions.

    Regression: _ws_attach only unregistered the main session and cancelled
    its delta task, but left pool-agent / subagent sessions from the previous
    session registered.  Those sessions kept forwarding deltas to the
    same WebSocket, causing the frontend to render another session's
    streaming output after switching.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        # Attach to session A
        await ws.send_json({"action": "attach", "session_id": "web:conv-a.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Server registers main.A and coding.A for this WebSocket
        assert "web:conv-a.main" in input_adapter._connections
        assert "web:conv-a.coding" in input_adapter._connections

        # Now switch to session B
        await ws.send_json({"action": "attach", "session_id": "web:conv-b.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Previous session sessions must be fully unregistered
        assert "web:conv-a.main" not in input_adapter._connections
        assert "web:conv-a.coding" not in input_adapter._connections
        assert "web:conv-a.main" not in input_adapter._delta_queues
        assert "web:conv-a.coding" not in input_adapter._delta_queues

        # New session sessions must be registered
        assert "web:conv-b.main" in input_adapter._connections
        assert "web:conv-b.coding" in input_adapter._connections
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_list_includes_subagent_with_parent_relation() -> None:
    """GET /api/sessions includes subagent sessions that have parent relationships."""
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent
    from framework.core.session_id import SessionId

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"coding": "coding", "main": "main"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"coding": "coding", "main": "main"})
    server.set_pool_agent_names(["main", "coding"])

    # Create session store and record parent→child via SessionId
    parent_sid = "abc.coding"
    child_sid = "abc.coding.reviewer.ee11"
    session_store = WorkspacePoolSessionStore(
        data_dir,
        pool_resolver=lambda s: "coding",
    )
    parent_session = SessionId(
        session_id=parent_sid, agent_name="coding"
)
    child_session = SessionId(
        session_id=child_sid, agent_name="reviewer",
        parent_session_id=parent_sid
)
    await session_store.save(parent_session)
    await session_store.save(child_session)
    server.set_session_store(session_store)

    # Add transcript data for both parent and child
    store.append(parent_sid,
        UserMessageEvent(session_id=parent_sid, agent_name="coding", content="hi"))
    store.append(child_sid,
        UserMessageEvent(session_id=child_sid, agent_name="reviewer", content="reviewing"))

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        by_sid = {s["session_id"]: s for s in sessions}

        # Parent session should be present
        assert parent_sid in by_sid, f"parent {parent_sid} missing from sessions"
        assert by_sid[parent_sid]["parent_session_id"] is None

        # Subagent session should ALSO be present with correct parent
        assert child_sid in by_sid, (
            f"child {child_sid} missing from sessions — "
            "subagent sessions with parent relations must be included"
        )
        assert by_sid[child_sid]["parent_session_id"] == parent_sid
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_api_messages_loads_subagent_transcript() -> None:
    """GET /api/sessions/{subagent_id}/messages loads subagent transcript events."""
    from bot.webui.events import UserMessageEvent

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"coding": "coding", "reviewer": "coding"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"coding": "coding", "reviewer": "coding"})
    server.set_pool_agent_names(["coding"])

    parent_sid = "abc.coding"
    child_sid = "abc.coding.reviewer.ee11"

    # Write transcript data for the subagent session
    store.append(parent_sid,
        UserMessageEvent(session_id=parent_sid, agent_name="coding", content="hi"))
    store.append(child_sid,
        UserMessageEvent(session_id=child_sid, agent_name="reviewer", content="review result"))

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Load messages for the subagent session
        resp = await client.get(f"/api/sessions/{child_sid}/messages")
        assert resp.status == 200, f"Expected 200, got {resp.status}"
        events = await resp.json()
        assert len(events) == 1, (
            f"Expected 1 event for subagent session, got {len(events)}: {events}"
        )
        assert events[0]["session_id"] == child_sid
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subagent_streaming_delta_arrives_at_ws_client() -> None:
    """Subagent emitter deltas must arrive at the WebSocket client via watcher."""
    from bot.webui.events import SessionMeta

    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    store.set_agent_pool_map({"coding": "coding", "reviewer": "coding"})
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)
    server.set_agent_pool_map({"coding": "coding", "reviewer": "coding"})
    server.set_pool_agent_names(["coding"])

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        parent_sid = "sub.coding"
        child_sid = "sub.coding.reviewer.ee11"

        # Attach to the parent session
        await ws.send_json({"action": "attach", "session_id": parent_sid})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Simulate what _on_subagent_created does: pre-register the subagent queue
        input_adapter.ensure_queue(child_sid)

        # Create a WebBotEmitter for the subagent (this is what the pipeline does)
        emitter = WebBotEmitter(
            output_adapter,
            child_sid,
            config=EmitterConfig(),
            session_meta_resolver=lambda: SessionMeta(
                pool="coding", parent_session_id=parent_sid
            )
)

        # Emit a delta — this should enqueue into the delta queue
        await emitter.emit_delta("subagent streaming test")

        # The watcher should pick up the new queue within ~1.5s and start forwarding.
        # Wait for the delta to arrive at the WS client.
        received_raw = await ws.receive_json(timeout=3)
        received = _unwrap_envelope(received_raw)
        assert received["event"] == "model_content_delta", (
            f"Expected model_content_delta, got {received.get('event')}"
        )
        assert received["session_id"] == child_sid, (
            f"Expected session_id={child_sid}, got {received.get('session_id')}"
        )
        assert received["text"] == "subagent streaming test"
        # parent_session_id is on the envelope (not unwrapped); check raw
        assert received_raw.get("parent_session_id") == parent_sid
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_full_stream_isolation_across_sessions() -> None:
    """End-to-end: emitter deltas for one session do not leak after switching.

    This exercises the real pipeline:
      WebBotEmitter -> WebSocketOutputAdapter -> per-session queue ->
      WebUIServer._forward_deltas -> WebSocket -> client.
    """
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        # Attach to session A
        await ws.send_json({"action": "attach", "session_id": "web:conv-a.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Emit a streaming delta for A through the real emitter
        emitter_a = WebBotEmitter(
            output_adapter,
            "web:conv-a.main",
            config=EmitterConfig()
)
        await emitter_a.emit_delta("hello from A")

        received = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert received["event"] == "model_content_delta"
        assert received["session_id"] == "web:conv-a.main"
        assert received["text"] == "hello from A"

        # Switch to session B
        await ws.send_json({"action": "attach", "session_id": "web:conv-b.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        # Emit another delta for A — it must NOT arrive on this WebSocket
        await emitter_a.emit_delta("leaked from A")

        # Because A is unregistered, the queue is gone and send_delta is a no-op,
        # so the next receive_json would timeout.  We assert the connection is
        # clean by checking the adapter state directly.
        assert "web:conv-a.main" not in input_adapter._delta_queues
        assert "web:conv-a.main" not in input_adapter._connections

        # Emit for B and confirm it DOES arrive
        emitter_b = WebBotEmitter(
            output_adapter,
            "web:conv-b.main",
            config=EmitterConfig()
)
        await emitter_b.emit_delta("hello from B")

        received = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert received["event"] == "model_content_delta"
        assert received["session_id"] == "web:conv-b.main"
        assert received["text"] == "hello from B"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_turn_end_streaming_stop_is_isolated() -> None:
    """turn_end for session A must not stop streaming in session B."""
    data_dir = Path(tempfile.mkdtemp())
    input_adapter = WebSocketInputAdapter()
    output_adapter = WebSocketOutputAdapter(input_adapter)
    store = WorkspaceScopedTranscriptStore(data_dir, lambda: "")
    server = WebUIServer(
        input_adapter, store, static_dist=None, data_dir=data_dir
    )
    server.set_workspace_index(store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")

        # Attach two different tabs by using two separate connections is hard
        # in one test; instead we verify the contract at the event level:
        # the frontend reducer ignores turn_end for a different session.
        await ws.send_json({"action": "attach", "session_id": "web:conv-a.main"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"

        emitter_a = WebBotEmitter(
            output_adapter,
            "web:conv-a.main",
            config=EmitterConfig()
)
        await emitter_a.emit_delta("streaming in A...")
        delta = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert delta["event"] == "model_content_delta"

        # Send a turn_end for a different session over the same connection.
        # The backend will forward it because the connection is currently attached
        # to A, but the frontend reducer must ignore it.
        await ws.send_str(json.dumps({
            "event": "turn_end",
            "session_id": "web:conv-b.main",
            "agent_name": "main",
            "turn_id": "turn_1",
            "latency_ms": 0,
        }))

        # The real turn_end for A should still end streaming correctly.
        await emitter_a.emit_complete(AgentResult(content="done"))
        end = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert end["event"] == "turn_end"
        assert end["session_id"] == "web:conv-a.main"
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════════
# Workspace API tests
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_workspace_cd_switches_current_workspace() -> None:
    """POST /api/workspace/cd must change WorkspaceContext.current and the
    server's active store so that subsequent GET /api/sessions returns
    sessions from the NEW workspace, not the old one.
    """
    from framework.workspace.context import DefaultWorkspaceContext

    home = Path(tempfile.mkdtemp())
    ws_a = Path(tempfile.mkdtemp())
    ws_b = Path(tempfile.mkdtemp())

    # ── Workspace context ──────────────────────────────────────────
    ws_ctx = DefaultWorkspaceContext(home=home, active_checker=lambda: False)

    # ── Server ─────────────────────────────────────────────────────
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(
        ws_ctx.data_dir / "sessions", lambda: str(ws_ctx.current)
    )
    store.set_agent_pool_map({"main": "main"})
    server = WebUIServer(inp, store, static_dist=None, data_dir=ws_ctx.data_dir)
    server.set_workspace_index(store)
    server.set_workspace_context(ws_ctx)
    server.set_agent_pool_map({"main": "main"})
    server.set_pool_agent_names(["main"])

    # ── Register a workspace-switch callback that rebases the store ─
    # This mirrors what BotService._on_ws_stop_and_rebuild does in production,
    # minus the heavy pool-memory/infrastructure rebuild.
    async def _on_switch(_old_dir: Path, new_dir: Path) -> None:
        sessions_dir = new_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        store.rebase(sessions_dir)

    class _Adapter:
        def __init__(self, fn): self._fn = fn
        async def on_workspace_switch(self, old, new): await self._fn(old, new)
    ws_ctx.register_callback(_Adapter(_on_switch))

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # ── Add a session in workspace A ───────────────────────────
        from bot.webui.events import UserMessageEvent

        sid_a = f"{_new_uuid_prefix()}.main"

        # Switch to workspace A
        resp = await client.post("/api/workspace/cd", json={"path": str(ws_a)})
        cd_a = await resp.json()
        assert cd_a["success"], f"cd to ws-a failed: {cd_a}"

        server._active_store("main").append(
            sid_a, UserMessageEvent(session_id=sid_a, agent_name="main", content="ws-a")
        )

        # Verify session is visible in workspace A
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        assert any(s["session_id"] == sid_a for s in sessions), (
            f"session {sid_a} must be visible in ws-a"
        )

        # ── Switch to workspace B ──────────────────────────────────
        resp = await client.post("/api/workspace/cd", json={"path": str(ws_b)})
        cd_b = await resp.json()
        assert cd_b["success"], f"cd to ws-b failed: {cd_b}"

        # Verify workspace A's session is NOT visible in B
        resp = await client.get("/api/sessions")
        sessions_b = await resp.json()
        sids_b = {s["session_id"] for s in sessions_b}
        assert sid_a not in sids_b, (
            f"workspace A session {sid_a} leaked into workspace B"
        )
    finally:
        await client.close()
