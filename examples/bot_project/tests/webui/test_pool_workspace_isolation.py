"""Regression tests for pool selection persistence and IM workspace isolation.

In the workspace-scoped model:
- Each workspace hash has its own transcript store and sessions.json.
- Switching workspace changes the active data source.
- Sessions created while workspace-A is active live in workspace-A's store,
  and are invisible when workspace-B is active.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.pool_router import PoolSessionStore
from bot.webui.server import WebUIServer, _make_session_id, _new_uuid_prefix, _workspace_sanitized
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.transcript_store import JSONLTranscriptStore


def _make_server(data_dir: Path) -> tuple[WebUIServer, WebSocketInputAdapter]:
    inp = WebSocketInputAdapter()
    holder: list = []
    def _ws_resolver() -> str:
        s = holder[0] if holder else None
        return str(s._workspace_ctx.current) if s is not None and s._workspace_ctx is not None else ""
    store = WorkspaceScopedTranscriptStore(data_dir, _ws_resolver)
    store.set_agent_pool_map({"main": "main", "coding": "coding"})
    server = WebUIServer(inp, store, static_dist=None, data_dir=data_dir)
    holder.append(server)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map({"main": "main", "coding": "coding"})
    return server, inp


def _make_callback(data_dir: Path):
    store = PoolSessionStore(data_dir=data_dir)
    called: list[tuple[str, str]] = []

    def set_pool(session_id: str, pool_name: str) -> None:
        store.set(session_id, pool_name)
        called.append((session_id, pool_name))

    return set_pool, store, called


# ── Issue 3: Pool selection must be fixed after creation ──────────────────


@pytest.mark.asyncio
async def test_pool_fixed_on_creation_not_overridden_by_attach() -> None:
    """When a session is created with pool=coding, subsequent WebSocket
    attach/send_message must NOT change the pool to 'main'.

    Regression: if pool_switch_callback is not called during attach,
    PoolSessionStore defaults to 'main' — silently losing the user's choice.
    """
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)
    callback, real_store, calls = _make_callback(data_dir)
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Create with coding pool via WS attach
        ws = await client.ws_connect("/ws")
        uuid = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": uuid, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        coding_sid = attached["session_id"]
        conv_id = uuid

        # Attach and send (triggers pool_switch_callback)

        await ws.send_json({
            "action": "send_message",
            "session_id": coding_sid,
            "content": "hello coding",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))

        # PoolSessionStore must return 'coding', NOT 'main'
        pool = real_store.get(conv_id, "main")
        assert pool == "coding", (
            f"Pool for conversation {conv_id!r} must be 'coding' "
            f"but PoolSessionStore returned {pool!r}. "
            f"Pool was overridden to 'main' after creation."
        )

        # Callback must have been called with the CORRECT pool
        coding_calls = [c for c in calls if c[0] == conv_id and c[1] == "coding"]
        assert len(coding_calls) >= 1, (
            f"pool_switch_callback must be called with ({conv_id!r}, 'coding'). "
            f"Got calls: {calls}"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pool_survives_multiple_attach_cycles() -> None:
    """Pool assignment must survive multiple WebSocket attach/detach cycles.

    User opens conv → sends → closes tab → reopens → pool must still be correct.
    """
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)
    callback, real_store, calls = _make_callback(data_dir)
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        uuid = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": uuid, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        coding_sid = attached["session_id"]
        conv_id = uuid

        # Cycle 1: attach → send → close
        await ws.send_json({
            "action": "send_message",
            "session_id": coding_sid,
            "content": "cycle-1",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))
        await ws.close()

        # Cycle 2: reattach → send
        ws2 = await client.ws_connect("/ws")
        await ws2.send_json({"action": "attach", "session_id": coding_sid})
        await ws2.receive_json()
        await ws2.send_json({
            "action": "send_message",
            "session_id": coding_sid,
            "content": "cycle-2",
        })
        await ws2.receive_json(timeout=2)

        # Pool must still be 'coding'
        pool = real_store.get(conv_id, "main")
        assert pool == "coding", (
            f"After 2 attach cycles, pool must still be 'coding', got {pool!r}"
        )
    finally:
        await client.close()


# ── Issue 4: Workspace-scoped session isolation ────────────────────────


@pytest.mark.asyncio
async def test_im_conversation_stored_in_current_workspace() -> None:
    """IM messages written while on the default workspace are visible there."""
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        from bot.webui.events import UserMessageEvent

        im_conv_id = "qq_user_999"
        im_sid = f"{im_conv_id}.main"
        event = UserMessageEvent(
            session_id=im_sid,
            agent_name="main",
            content="QQ message from user",
        )
        server._store.append(im_sid, event)

        # GET /api/sessions must include it
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert im_conv_id in conv_ids, (
            f"IM session {im_conv_id!r} must be visible in session list"
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sessions_from_different_workspaces_are_isolated() -> None:
    """Sessions written while in workspace-A must NOT appear in workspace-B."""
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)

    # Use real temp directories as workspace paths
    ws_a = str(Path(data_dir) / "ws-a")
    ws_b = str(Path(data_dir) / "ws-b")
    Path(ws_a).mkdir(exist_ok=True)
    Path(ws_b).mkdir(exist_ok=True)

    # Simulate workspace context starting at ws-a
    ws_ctx = MagicMock()
    ws_ctx.current = Path(ws_a)
    ws_ctx.home = data_dir
    server.set_workspace_context(ws_ctx)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        from bot.webui.events import UserMessageEvent

        # Create session in workspace-a (direct store append, no API needed)
        sid_a = f"{_new_uuid_prefix()}.main"
        conv_a = sid_a.split(".")[0]

        event_a = UserMessageEvent(
            session_id=sid_a, agent_name="main", content="ws-a msg",
        )
        server._store.append(sid_a, event_a)

        # Switch to workspace-b and create a session
        ws_ctx.current = Path(ws_b)
        sid_b = f"{_new_uuid_prefix()}.main"
        conv_b = sid_b.split(".")[0]

        event_b = UserMessageEvent(
            session_id=sid_b, agent_name="main", content="ws-b msg",
        )
        server._store.append(sid_b, event_b)

        # In workspace-b, only conv_b is visible
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert conv_b in conv_ids, "conv-b must be visible in ws-b"
        assert conv_a not in conv_ids, (
            f"conv-a (workspace={ws_a!r}) must NOT appear in ws-b"
        )

        # Switch back to workspace-a
        ws_ctx.current = Path(ws_a)
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert conv_a in conv_ids, "conv-a must be visible in ws-a"
        assert conv_b not in conv_ids, (
            f"conv-b (workspace={ws_b!r}) must NOT appear in ws-a"
        )
    finally:
        await client.close()
