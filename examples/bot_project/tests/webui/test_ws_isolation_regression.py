"""Regression repro for the workspace-isolation bugs reported 2026-06-21.

Three symptoms:
  1. 前端会话管理查不出来用户发的消息内容, 完全丢失
     (user-message content is lost when fetched).
  2. 会话管理错乱: 切到 home 仍能看到其他 ws 的会话记录 (cross-ws leakage).
  3. 点击 ws 下的 recent, 空屏 (a ws's sessions / history are blank).

These tests drive the REAL WebUIServer + store + session index the way the
frontend does (``fetchMessages`` sends NO ``?ws=``; ``fetchSessions`` sends
``?ws=`` only when a workspace is active) and assert the *correct* ws-isolated
behaviour.  Failures == the bugs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent, _unwrap_envelope
from bot.webui.server import WebUIServer
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.workspace.runtime import bind_workspace_root

_DATA_DIR_NAME = ".modex"


def _build_server(home: Path):
    input_adapter = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    store.set_agent_pool_map({"main": "main", "coding": "coding"})
    home_sessions_dir = WorkspacePaths(root=home / _DATA_DIR_NAME).sessions_dir
    server = WebUIServer(
        input_adapter,
        store,
        static_dist=None,
        data_dir=home,
        home_sessions_dir=home_sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(_DATA_DIR_NAME)
    server.set_agent_pool_map({"main": "main", "coding": "coding"})
    server.set_pool_agent_names(["main", "coding"])
    server.set_session_factory(SessionIdFactory())
    agent_pool_map = {"main": "main", "coding": "coding"}
    server.set_session_store(
        WorkspacePoolSessionStore(
            base_dir=home,
            pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
        )
    )
    return server, store


def _seed(store, ws_root: Path, session_id: str, content: str) -> None:
    """Append a user message under *ws_root* (routes via the ctxvar root)."""
    with bind_workspace_root(ws_root):
        store.append(
            session_id,
            UserMessageEvent(session_id=session_id, agent_name="main", content=content),
        )


@pytest.mark.asyncio
async def test_home_does_not_leak_other_ws_sessions() -> None:
    """BUG 2: listing home must NOT show sessions created under another ws."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()
        server, store = _build_server(home)

        sid_home = "convH.main"
        sid_a = "convA.main"
        _seed(store, home, sid_home, "msg in home")
        _seed(store, ws_a, sid_a, "msg in ws_a")

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            # Home listing (frontend: fetchSessions(workspace || undefined) with
            # workspace empty -> NO ?ws=).
            home_list = await (await client.get("/api/sessions")).json()
            home_ids = {s["session_id"] for s in home_list}
            assert sid_home in home_ids, f"home should list its own session; got {home_ids}"
            assert sid_a not in home_ids, (
                f"BUG2: home leaked ws_a session {sid_a}; home_ids={home_ids}"
            )

            # ws_a listing.
            wsa_list = await (
                await client.get(f"/api/sessions?ws={ws_a}")
            ).json()
            wsa_ids = {s["session_id"] for s in wsa_list}
            assert sid_a in wsa_ids, f"BUG3: ws_a should list its own session; got {wsa_ids}"
            assert sid_home not in wsa_ids, (
                f"BUG2: ws_a leaked home session {sid_home}; wsa_ids={wsa_ids}"
            )
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_message_roundtrip_with_ws() -> None:
    """BUG 1 (backend honours ws on read): a message written under ws_a must be
    retrievable when the reader passes ?ws=ws_a."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()
        server, store = _build_server(home)

        sid_a = "convA.main"
        _seed(store, ws_a, sid_a, "hello from ws_a")

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            events = await (
                await client.get(f"/api/sessions/{sid_a}/messages?ws={ws_a}")
            ).json()
            user_msgs = [e for e in events if e.get("event") == "user_message"]
            assert any("ws_a" in e.get("content", "") for e in user_msgs), (
                f"BUG1: ws_a message not found when reading with ?ws=ws_a; events={events}"
            )
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_message_lost_without_ws_frontend_behaviour() -> None:
    """BUG 1 (frontend): fetchMessages sends NO ?ws=. A message written under a
    non-home ws is therefore unreadable. This documents the symptom the user
    sees (lost content) and what the fix must restore: pass ws on read."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()
        server, store = _build_server(home)

        sid_a = "convA.main"
        _seed(store, ws_a, sid_a, "hello from ws_a")

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            # Exactly what the frontend does today: GET /messages with NO ws.
            events = await (
                await client.get(f"/api/sessions/{sid_a}/messages")
            ).json()
            user_msgs = [e for e in events if e.get("event") == "user_message"]
            # The message is NOT found today (reads home, not ws_a).
            found = any("ws_a" in e.get("content", "") for e in user_msgs)
            assert found is False, (
                "precondition: without ?ws= the server reads home and does NOT see "
                f"ws_a's message (if this passes, Bug1 root cause differs); got {user_msgs}"
            )
            # The fix (frontend passes ws) must restore it:
            events_ws = await (
                await client.get(f"/api/sessions/{sid_a}/messages?ws={ws_a}")
            ).json()
            user_msgs_ws = [e for e in events_ws if e.get("event") == "user_message"]
            assert any("ws_a" in e.get("content", "") for e in user_msgs_ws), (
                "after the frontend passes ws, the message must be readable"
            )
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_send_message_then_attach_under_ws_finds_history() -> None:
    """BUG 3: after sending a message under ws_a (real pipeline write), the ws_a
    session must be discoverable and its history loadable under ws_a."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        ws_a = Path(tmp) / "ws_a"
        ws_a.mkdir()
        server, store = _build_server(home)
        from tests.webui._pipeline_fixture import attach_default_pipeline

        attach_default_pipeline(
            server, store, server._input, workspace_root=home,
            agent_pool_map={"main": "main", "coding": "coding"},
        )

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            # Attach a NEW conversation under ws_a (frontend passes ws).
            await ws.send_json(
                {"action": "attach", "uuid_prefix": "convA", "pool": "main", "ws": str(ws_a)}
            )
            attached = _unwrap_envelope(await ws.receive_json())
            assert attached["event"] == "attached"
            sid = attached["session_id"]

            # Send a message under ws_a.
            await ws.send_json(
                {"action": "send_message", "session_id": sid, "content": "hi ws_a", "ws": str(ws_a)}
            )
            echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
            assert echoed["event"] == "user_message"
            await ws.close()

            # ws_a session list must include it (Bug 3: "recent blank").
            wsa_list = await (
                await client.get(f"/api/sessions?ws={ws_a}")
            ).json()
            wsa_ids = {s["session_id"] for s in wsa_list}
            assert sid in wsa_ids, (
                f"BUG3: session created under ws_a missing from ws_a list; got {wsa_ids}"
            )

            # BUG 2: a session created under ws_a (indexed via the materialize
            # path into the GLOBAL session store) must NOT appear in the HOME
            # listing. The session index is global today, so home leaks it.
            home_list = await (await client.get("/api/sessions")).json()
            home_ids = {s["session_id"] for s in home_list}
            assert sid not in home_ids, (
                f"BUG2: home leaked a ws_a session ({sid}); home_ids={home_ids}"
            )

            # And its message must be readable under ws_a.
            events = await (
                await client.get(f"/api/sessions/{sid}/messages?ws={ws_a}")
            ).json()
            user_msgs = [e for e in events if e.get("event") == "user_message"]
            assert any("ws_a" in e.get("content", "") for e in user_msgs), (
                f"BUG1: message sent under ws_a not readable under ws_a; got {events}"
            )
        finally:
            await client.close()


def _seed_for_pool(store, ws_root: Path, session_id: str, content: str) -> None:
    """Append a user message for a specific agent under *ws_root*."""
    agent_name = session_id.split(".")[1] if "." in session_id else "main"
    with bind_workspace_root(ws_root):
        store.append(
            session_id,
            UserMessageEvent(session_id=session_id, agent_name=agent_name, content=content),
        )


@pytest.mark.asyncio
async def test_get_messages_does_not_leak_across_pools() -> None:
    """BUG: GET /api/sessions/{prefix}.{pool}/messages must only return messages
    from that specific pool, not from other pools sharing the same conversation
    prefix.  For example, session 'convA.coding' must NOT include messages from
    'convA.main' even though they share the prefix 'convA'.

    This is the cross-pool leakage bug in the messages API."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        server, store = _build_server(home)

        conv_prefix = "convA"
        sid_main = f"{conv_prefix}.main"
        sid_coding = f"{conv_prefix}.coding"

        _seed_for_pool(store, home, sid_main, "hello from main pool")
        _seed_for_pool(store, home, sid_coding, "hello from coding pool")

        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            main_events = await (
                await client.get(f"/api/sessions/{sid_main}/messages")
            ).json()
            main_msgs = [e for e in main_events if e.get("event") == "user_message"]
            main_contents = {e.get("content", "") for e in main_msgs}
            assert "hello from main pool" in main_contents, (
                f"main session should contain its own message; got {main_contents}"
            )
            assert "hello from coding pool" not in main_contents, (
                f"BUG: main session leaked coding pool message; got {main_contents}"
            )

            coding_events = await (
                await client.get(f"/api/sessions/{sid_coding}/messages")
            ).json()
            coding_msgs = [e for e in coding_events if e.get("event") == "user_message"]
            coding_contents = {e.get("content", "") for e in coding_msgs}
            assert "hello from coding pool" in coding_contents, (
                f"coding session should contain its own message; got {coding_contents}"
            )
            assert "hello from main pool" not in coding_contents, (
                f"BUG: coding session leaked main pool message; got {coding_contents}"
            )
        finally:
            await client.close()
