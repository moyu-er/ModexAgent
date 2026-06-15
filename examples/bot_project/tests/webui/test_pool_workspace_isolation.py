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
from bot.webui.server import WebUIServer, _new_uuid_prefix
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent, _unwrap_envelope
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

    # Inject the WebUI input pipeline so _ws_send_message works.
    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, server._store, inp, pool_session_store=real_store)

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

        # Attach and send (S5 persists explicit_pool into ctx.pool_session_store)

        await ws.send_json({
            "action": "send_message",
            "session_id": coding_sid,
            "content": "hello coding",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))

        # S5 persists explicit_pool directly into the real PoolSessionStore.
        pool = real_store.get(conv_id, "main")
        assert pool == "coding", (
            f"Pool for conversation {conv_id!r} must be 'coding' "
            f"but PoolSessionStore returned {pool!r}. "
            f"Pool was overridden to 'main' after creation."
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

    # Inject the WebUI input pipeline so _ws_send_message works.
    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, server._store, inp, pool_session_store=real_store)

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
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent
    from framework.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)

    agent_pool_map = {"main": "main", "coding": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        im_conv_id = "qq_user_999"
        im_sid = f"{im_conv_id}.main"
        event = UserMessageEvent(
            session_id=im_sid,
            agent_name="main",
            content="QQ message from user"
)
        server._store.append(im_sid, event)

        # Save IM session to the session store.
        await session_store.save(SessionInfo(
            session_id=im_sid,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

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
    """Sessions written while in workspace-A must NOT appear in workspace-B.

    In the new model each workspace has its own data dir (``.modex/sessions/``),
    so sessions are physically separate.
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent
    from framework.core.session_id import SessionInfo, now_ms

    data_dir_a = Path(tempfile.mkdtemp())
    data_dir_b = Path(tempfile.mkdtemp())

    server, inp = _make_server(data_dir_a)

    ws_ctx = MagicMock()
    ws_ctx.current = Path("/ws-a")
    ws_ctx.home = Path("/ws-a")
    server.set_workspace_context(ws_ctx)

    agent_pool_map = {"main": "main", "coding": "coding"}
    session_store_a = WorkspacePoolSessionStore(
        base_dir=data_dir_a,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store_a)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        sid_a = f"{_new_uuid_prefix()}.main"
        conv_a = sid_a.split(".")[0]

        event_a = UserMessageEvent(
            session_id=sid_a, agent_name="main", content="ws-a msg"
)
        server._store.append(sid_a, event_a)

        # Save session_a to workspace A's session store.
        await session_store_a.save(SessionInfo(
            session_id=sid_a,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        # Switch to workspace-b: recreate store with new data dir
        server._store = WorkspaceScopedTranscriptStore(data_dir_b, lambda: str(ws_ctx.current))
        server._store.set_agent_pool_map(agent_pool_map)
        if server._workspace_index is not None:
            server.set_workspace_index(server._store)

        # Switch session store to workspace B.
        session_store_b = WorkspacePoolSessionStore(
            base_dir=data_dir_b,
            pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
        )
        server.set_session_store(session_store_b)

        sid_b = f"{_new_uuid_prefix()}.main"
        conv_b = sid_b.split(".")[0]

        event_b = UserMessageEvent(
            session_id=sid_b, agent_name="main", content="ws-b msg"
)
        server._store.append(sid_b, event_b)

        # Save session_b to workspace B's session store.
        await session_store_b.save(SessionInfo(
            session_id=sid_b,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        # In workspace-b, only conv_b is visible
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert conv_b in conv_ids, "conv-b must be visible in ws-b"
        assert conv_a not in conv_ids, "conv-a must NOT appear in ws-b"

        # Switch back to workspace-a
        server._store = WorkspaceScopedTranscriptStore(data_dir_a, lambda: str(ws_ctx.current))
        server._store.set_agent_pool_map(agent_pool_map)
        if server._workspace_index is not None:
            server.set_workspace_index(server._store)

        # Switch session store back to workspace A.
        server.set_session_store(session_store_a)

        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert conv_a in conv_ids, "conv-a must be visible in ws-a"
        assert conv_b not in conv_ids, "conv-b must NOT appear in ws-a"
    finally:
        await client.close()


def test_append_follows_current_workspace_after_switch() -> None:
    """New writes always go to the CURRENT workspace, not a sticky one.

    Regression: IM (QQ) sessions were locked to the workspace where the
    first message arrived.  After ``cd``, subsequent writes must go to
    the new workspace so the frontend can discover them in the session list.
    """
    data_dir = Path(tempfile.mkdtemp())
    _ws: list[str] = ["/home/bot_project"]

    def _resolver() -> str:
        return _ws[0]

    store = WorkspaceScopedTranscriptStore(data_dir, _resolver)
    store.set_agent_pool_map({"main": "main"})
    sid = "conv-q1.main"

    # 1. Write in workspace A (default)
    store.append(sid, UserMessageEvent(
        session_id=sid, agent_name="main", content="msg-in-home"
))

    # 2. Switch to workspace B (simulating cd E:\\download\\bot)
    _ws[0] = "E:\\download\\bot"

    # 3. Write another event for same session — must go to workspace B
    store.append(sid, UserMessageEvent(
        session_id=sid, agent_name="main", content="msg-after-cd"
))

    # 4. Verify the second write went to workspace B (CURRENT), not A (sticky)
    events_b = list(JSONLTranscriptStore(data_dir / "main").load(sid))
    assert len(events_b) >= 1, (
        f"Expected events in workspace B, but none found. "
        "append() must use CURRENT workspace, not sticky."
    )
    assert any("msg-after-cd" in str(e.to_dict()) for e in events_b), (
        "msg-after-cd must appear in workspace B"
    )

    # 5. Previous message still intact in workspace A
    events_a = list(JSONLTranscriptStore(data_dir / "main").load(sid))
    assert any("msg-in-home" in str(e.to_dict()) for e in events_a), (
        "msg-in-home must still be in workspace A"
    )


def test_append_follows_repeated_workspace_switches() -> None:
    """Repeated cd switches correctly route writes to the current workspace."""
    data_dir = Path(tempfile.mkdtemp())
    _ws: list[str] = ["/home"]

    def _resolver() -> str:
        return _ws[0]

    store = WorkspaceScopedTranscriptStore(data_dir, _resolver)
    store.set_agent_pool_map({"main": "main"})
    sid = "conv-r1.main"

    # W1 → A
    store.append(sid, UserMessageEvent(
        session_id=sid, agent_name="main", content="w1-in-A"
))

    # cd B, W2 → B
    _ws[0] = "/workspace-b"
    store.append(sid, UserMessageEvent(
        session_id=sid, agent_name="main", content="w2-in-B"
))

    # cd C, W3 → C
    _ws[0] = "/workspace-c"
    store.append(sid, UserMessageEvent(
        session_id=sid, agent_name="main", content="w3-in-C"
))

    # cd back to A, W4 → A
    _ws[0] = "/home"
    store.append(sid, UserMessageEvent(
        session_id=sid, agent_name="main", content="w4-back-in-A"
))

    # Verify each workspace has correct events
    # data_dir is already workspace-specific; use pool dirs directly
    for _ws_path, expected_content in [
        ("/home", "w1-in-A"),
        ("/home", "w4-back-in-A"),
        ("/workspace-b", "w2-in-B"),
        ("/workspace-c", "w3-in-C"),
    ]:
        events = list(JSONLTranscriptStore(data_dir / "main").load(sid))
        assert any(expected_content in str(e.to_dict()) for e in events), (
            f"Expected {expected_content!r} in data_dir"
        )


def test_relation_store_follows_workspace_switch() -> None:
    """WorkspacePoolSessionStore writes sessions to the CURRENT workspace."""
    import asyncio

    from bot.service.session_store import WorkspacePoolSessionStore
    from framework.core.session_id import SessionInfo

    data_dir = Path(tempfile.mkdtemp())
    _ws: list[str] = ["home"]

    def _resolver() -> str:
        return _ws[0]

    store = WorkspacePoolSessionStore(
        data_dir,
        pool_resolver=lambda s: "coding",
    )

    parent = "conv.coding"
    child = "conv.coding.reviewer.ee11"

    # Write in workspace A
    child_session = SessionInfo(
        session_id=child, agent_name="reviewer",
        parent_session_id=parent
)
    asyncio.run(store.save(child_session))
    retrieved = asyncio.run(store.get(child))
    assert retrieved is not None
    assert retrieved.parent_session_id == parent

    # Switch to workspace B, write another relation
    _ws[0] = "workspace-b"
    child2 = "conv.coding.reviewer.ff22"
    child2_session = SessionInfo(
        session_id=child2, agent_name="reviewer",
        parent_session_id=parent
)
    asyncio.run(store.save(child2_session))
    retrieved2 = asyncio.run(store.get(child2))
    assert retrieved2 is not None
    assert retrieved2.parent_session_id == parent

    # Switch back to A
    _ws[0] = "home"
    # The first session should still be found (all workspaces scanned via glob)
    retrieved_a = asyncio.run(store.get(child))
    assert retrieved_a is not None
    assert retrieved_a.parent_session_id == parent
    # And a new write goes to A
    child3 = "conv.coding.reviewer.gg33"
    child3_session = SessionInfo(
        session_id=child3, agent_name="reviewer",
        parent_session_id=parent
)
    asyncio.run(store.save(child3_session))
    retrieved3 = asyncio.run(store.get(child3))
    assert retrieved3 is not None
    assert retrieved3.parent_session_id == parent

    # Verify session JSON files exist under the pool directory
    assert list((data_dir / "coding").glob("*.json")), (
        "session JSON files missing under coding pool"
    )


def test_transcript_store_rebase_on_workspace_restore() -> None:
    """After workspace restore at startup, rebase must route writes to the
    restored workspace — not the stale project-home directory.

    Regression: ``WorkspaceScopedTranscriptStore`` is created in
    ``WebUIService.__init__`` with ``_base`` pointing at the project home.
    If the workspace is restored to a different directory during
    ``initialize()`` and ``rebase()`` is never called, subsequent writes
    (via S7 ``PersistUserMessageStage``) and reads (via
    ``_handle_sessions``) target the wrong workspace.
    """
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as home_tmp, TemporaryDirectory() as restored_tmp:
        home_dir = Path(home_tmp)
        restored_dir = Path(restored_tmp)
        sessions_dir_home = home_dir / "sessions"
        sessions_dir_restored = restored_dir / "sessions"

        # ── Simulate startup: store created at project home ──
        store = WorkspaceScopedTranscriptStore(sessions_dir_home, lambda: str(restored_dir))
        store.set_agent_pool_map({"main": "main"})
        sid = "conv1.main"

        # ── Write through store BEFORE rebase (pre-fix behavior) ──
        store.append(sid, UserMessageEvent(
            session_id=sid, agent_name="main", content="before-rebase"
))
        # BUG: write goes to stale _base (home), NOT the restored workspace
        home_events = list(JSONLTranscriptStore(sessions_dir_home / "main").load(sid))
        assert len(home_events) == 1 and "before-rebase" in str(home_events[0].to_dict()), (
            "before rebase: write must go to _base (home) — this is the stale path"
        )
        restored_events = list(JSONLTranscriptStore(sessions_dir_restored / "main").load(sid))
        assert restored_events == [], (
            "before rebase: restored workspace must be empty — _base was never updated"
        )

        # ── NOW rebase (simulating the fix) ──
        store.rebase(sessions_dir_restored)

        # ── Write AFTER rebase ──
        store.append(sid, UserMessageEvent(
            session_id=sid, agent_name="main", content="after-rebase"
))
        # Verify write goes to the restored workspace
        restored_events2 = list(JSONLTranscriptStore(sessions_dir_restored / "main").load(sid))
        assert len(restored_events2) == 1 and "after-rebase" in str(restored_events2[0].to_dict()), (
            "after rebase: write must go to restored workspace"
        )
        # Verify home is unchanged
        home_events2 = list(JSONLTranscriptStore(sessions_dir_home / "main").load(sid))
        assert len(home_events2) == 1, (
            "after rebase: home must still have only the pre-rebase event"
        )
