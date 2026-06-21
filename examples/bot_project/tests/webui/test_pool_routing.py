"""End-to-end tests: pool switching, control command interception, multi-channel.

These tests simulate real user behavior through the WebUI — creating
conversations with pool selection, sending messages, and verifying the
PoolRouter routes to the correct pool.  No real LLM calls.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.fan_in import FanInInputAdapter
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.pool_router import PoolRouter, PoolSessionStore
from bot.webui.server import (
    WebUIServer,
    _new_uuid_prefix,
)
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import _unwrap_envelope
from bot.webui.transcript_store import JSONLTranscriptStore
from framework.workspace.paths import WorkspacePaths
from framework.workspace.runtime import bind_workspace_root

# ── Helpers ────────────────────────────────────────────────────────────────

def _make_server(data_dir: Path) -> tuple[WebUIServer, WebSocketInputAdapter]:
    """Create a WebUIServer with both main and coding pools registered."""
    inp = WebSocketInputAdapter()
    holder: list = []
    def _ws_resolver() -> str:
        s = holder[0] if holder else None
        return str(s._workspace_control.current) if s is not None and s._workspace_control is not None else ""
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store.set_agent_pool_map({"main": "main", "coding": "coding"})
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    holder.append(server)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])
    server.set_agent_pool_map({"main": "main", "coding": "coding"})
    return server, inp


def _make_real_callback(data_dir: Path):
    """Return a callback that writes to a real PoolSessionStore.

    This is what pool_router.set_pool does in production.
    """
    store = PoolSessionStore(data_dir=data_dir)
    called: list[tuple[str, str]] = []

    def set_pool(session_id: str, pool_name: str) -> None:
        store.set(session_id, pool_name)
        called.append((session_id, pool_name))

    return set_pool, store, called


# ── Test 1: Pool switch — full user flow (POST → attach → send → route) ──


@pytest.mark.asyncio
async def test_pool_switch_full_flow_routes_to_coding() -> None:
    """User creates conversation with pool=coding, sends message →
    PoolSessionStore returns 'coding' for that conversation.

    Simulates: WS attach {uuid_prefix, pool:"coding"} → WS send_message
    """
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)

    # Wire a real PoolSessionStore callback (as pool_router.set_pool would)
    callback, real_store, calls = _make_real_callback(data_dir)
    server.set_pool_switch_callback(callback)

    # Inject the input pipeline with the real PoolSessionStore so S5
    # persists the UI pool choice into the same store.
    from tests.webui._pipeline_fixture import attach_default_pipeline
    # _make_server uses a holder pattern to get the workspace; unwrap
    store = server._store
    attach_default_pipeline(server, store, inp, pool_session_store=real_store, workspace_root=data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        # ── Step 1+2: WS attach with uuid_prefix+pool (creates + attaches) ──
        conv_id = _new_uuid_prefix()
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "uuid_prefix": conv_id, "pool": "coding"})
        attached = _unwrap_envelope(await ws.receive_json())
        assert attached["event"] == "attached"
        session_id = attached["session_id"]

        # Callback must have been called during attach (with snowflake, pool_name)
        assert len(calls) >= 1, "pool_switch_callback must be called during attach"
        assert calls[-1] == (conv_id, "coding"), (
            f"attach must call set_pool({conv_id!r}, 'coding'), got {calls[-1]}"
        )

        # ── Step 3: Send message ──
        await ws.send_json({
            "action": "send_message",
            "session_id": session_id,
            "content": "hello coding pool",
        })
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))

        # Echo must show correct agent_name (derived from stored pool)
        assert echoed["event"] == "user_message"
        assert echoed["agent_name"] == "coding", (
            f"Echoed agent_name must be 'coding', got {echoed['agent_name']!r}"
        )

        # S5 persists explicit_pool directly into pool_session_store;
        # the callback is no longer called by send_message.
        # The store is keyed by the agent-independent snowflake (conv_id).
        assert real_store.get(conv_id, "main") == "coding", (
            f"S5 must persist pool=coding for conversation {conv_id}"
        )

        # ── Step 4: Verify PoolSessionStore (simulates PoolRouter.run()) ──
        msg = inp._message_queue.get_nowait()
        sid = str(msg.session)
        # PoolSessionStore keys by snowflake (agent-independent), not full session_id
        target_pool = real_store.get(msg.session.session_id_prefix, "main")
        assert target_pool == "coding", (
            f"PoolRouter must route session {sid!r} to 'coding', "
            f"but PoolSessionStore returned {target_pool!r}"
        )
    finally:
        await client.close()


# ── Test 2: Without callback → PoolRouter falls back to default ──


@pytest.mark.asyncio
async def test_no_callback_defaults_to_main() -> None:
    """Regression test: if pool_switch_callback is never set (old bug),
    PoolRouter defaults to 'main' regardless of what _conv_meta says.

    This proves the callback is ESSENTIAL — without it, pool='coding'
    is stored in memory but PoolRouter never learns about it.
    """
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)

    # Deliberately DO NOT set pool_switch_callback (simulates the old bug)
    # server.set_pool_switch_callback(...)  ← MISSING

    # Inject pipeline (uses a MagicMock pool_session_store)
    from tests.webui._pipeline_fixture import attach_default_pipeline
    store = server._store
    attach_default_pipeline(server, store, inp, workspace_root=data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        # Create with pool=coding
        resp = await client.post("/api/sessions", json={"pool": "coding"})
        coding_sid = (await resp.json())["session_id"]

        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": coding_sid})
        _unwrap_envelope(await ws.receive_json())  # attached

        await ws.send_json({
            "action": "send_message",
            "session_id": coding_sid,
            "content": "test",
        })
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))

        # Echo shows 'coding' (explicit_pool resolved from agent_pool_map)
        assert echoed["agent_name"] == "coding"

        # But the PoolSessionStore from disk was NEVER notified → returns default 'main'
        msg = inp._message_queue.get_nowait()
        store = PoolSessionStore(data_dir=data_dir)
        target = store.get(str(msg.session), "main")
        assert target == "main", (
            f"WITHOUT callback, PoolRouter defaults to 'main', "
            f"but PoolSessionStore returned {target!r} — callback must be set"
        )
    finally:
        await client.close()


# ── Test 3: Control command interception on WebSocket adapter ──


@pytest.mark.asyncio
async def test_control_command_intercepted_before_enqueue() -> None:
    """Slash commands (/stop) must be intercepted by _try_intercept_control.

    /cd and /exit are now handled directly by S2 (EnvironmentControlStage),
    not by _try_intercept_control. This test verifies /stop interception.
    """
    from framework.commands.handlers import build_default_builtin_handlers
    from framework.commands.processor import SlashCommandProcessor
    from framework.control.channel import InMemoryControlChannel

    handlers = list(build_default_builtin_handlers())
    processor = SlashCommandProcessor(handlers=handlers)
    channel = InMemoryControlChannel()

    # Configure a raw WebSocketInputAdapter
    inp = WebSocketInputAdapter()
    inp.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=None,  # output not needed for interception test
    )

    # Verify configuration took effect
    assert inp._cmd_processor is not None, (
        "After configure_control_filter, _cmd_processor must NOT be None"
    )
    assert inp._control_channel is not None, (
        "After configure_control_filter, _control_channel must NOT be None"
    )

    # Test /stop interception
    result = await inp._try_intercept_control("/stop", "test-session.main")
    assert result is True, (
        f"/stop must be intercepted, got {result}"
    )

    # Test normal message — NOT intercepted
    result = await inp._try_intercept_control("hello world", "test-session.main")
    assert result is False, (
        f"Normal message must NOT be intercepted, got {result}"
    )

    # Verify /stop added a control command to the channel
    from framework.control.types import ControlCommandType, ControlScope

    cmds = await channel.drain(
        ControlScope(session_id="test-session.main"),
        command_types={ControlCommandType.CANCEL_TURN},
    )
    assert len(cmds) > 0, "/stop must produce a control command"
    assert cmds[0].type == ControlCommandType.CANCEL_TURN


# ── Test 4: FanInInputAdapter propagates control filter ──


@pytest.mark.asyncio
async def test_fan_in_propagates_control_filter_to_sources() -> None:
    """When configure_control_filter is called on FanInInputAdapter,
    ALL source adapters must receive the configuration.

    This is the fix for: terminal/process/command broken because
    WebSocketInputAdapter._cmd_processor was never set.
    """
    from framework.commands.processor import SlashCommandProcessor
    from framework.control.channel import InMemoryControlChannel

    # Create FanIn with WebSocket source
    fan_in = FanInInputAdapter()
    ws_source = WebSocketInputAdapter()
    fan_in.add_source(ws_source)

    # Before configure: source has no filter
    assert ws_source._cmd_processor is None, (
        "Before configure_control_filter, source._cmd_processor must be None"
    )
    assert ws_source._control_channel is None, (
        "Before configure_control_filter, source._control_channel must be None"
    )

    # Configure on FanIn — MUST propagate to source
    proc = SlashCommandProcessor.default()
    chan = InMemoryControlChannel()
    fan_in.configure_control_filter(
        control_channel=chan,
        command_processor=proc,
        output_adapter=None,
    )

    # After configure: source HAS filter
    assert ws_source._cmd_processor is not None, (
        "After FanIn.configure_control_filter, source._cmd_processor "
        "must NOT be None — propagation failed"
    )
    assert ws_source._control_channel is not None, (
        "After FanIn.configure_control_filter, source._control_channel "
        "must NOT be None — propagation failed"
    )

    # Verify interception actually works on the source
    result = await ws_source._try_intercept_control("/stop", "test.main")
    assert result is True, (
        f"After propagation, /stop must be intercepted on source, got {result}"
    )


# ── Test 5: Multiple sources — configure propagates to ALL ──


@pytest.mark.asyncio
async def test_fan_in_propagates_to_all_sources() -> None:
    """FanIn with 2+ sources — ALL get configured."""
    from framework.commands.processor import SlashCommandProcessor
    from framework.control.channel import InMemoryControlChannel

    fan_in = FanInInputAdapter()
    ws1 = WebSocketInputAdapter()
    ws2 = WebSocketInputAdapter()
    fan_in.add_source(ws1)
    fan_in.add_source(ws2)

    fan_in.configure_control_filter(
        control_channel=InMemoryControlChannel(),
        command_processor=SlashCommandProcessor.default(),
        output_adapter=None,
    )

    assert ws1._cmd_processor is not None, "Source 1 must be configured"
    assert ws2._cmd_processor is not None, "Source 2 must be configured"
    assert ws1._control_channel is not None, "Source 1 must have control_channel"
    assert ws2._control_channel is not None, "Source 2 must have control_channel"


# ── Test 6: Pool mapping persistence across server restart ──


@pytest.mark.asyncio
async def test_pool_mapping_survives_server_recreation() -> None:
    """Pool mapping saved to disk must survive server restart."""
    from bot.service.session_store import WorkspacePoolSessionStore
    from framework.core.session_id import SessionIdFactory

    data_dir = Path(tempfile.mkdtemp())
    agent_pool_map = {"main": "main", "coding": "coding"}
    factory = SessionIdFactory()

    # First server instance: create a session via API and send a message so
    # the transcript is persisted (empty sessions are not persisted).
    inp1 = WebSocketInputAdapter()
    store1 = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store1.set_agent_pool_map(agent_pool_map)
    server1 = WebUIServer(
        inp1,
        store1,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server1.set_workspace_index(store1)
    server1.set_agent_pool_map(agent_pool_map)
    server1.set_pool_agent_names(["main", "coding"])
    session_store1 = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server1.set_session_store(session_store1)
    server1.set_session_factory(factory)
    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server1, store1, inp1, workspace_root=data_dir)
    client1 = TestClient(TestServer(server1.app))
    await client1.start_server()
    try:
      with bind_workspace_root(data_dir):
        resp = await client1.post("/api/sessions", json={"pool": "coding"})
        session_id = (await resp.json())["session_id"]
        conv_id = session_id.split(".")[0]

        ws = await client1.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": session_id})
        _unwrap_envelope(await ws.receive_json())
        await ws.send_json({
            "action": "send_message",
            "session_id": session_id,
            "content": "hello coding",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))
    finally:
        await client1.close()

    # Verify transcript file exists under the coding pool directory.
    transcript_file = data_dir / ".modex" / "sessions" / "coding" / f"{conv_id}.coding.jsonl"
    assert transcript_file.exists()

    # No sessions.json in the new design.
    meta_file = data_dir / ".modex" / "sessions.json"
    assert not meta_file.exists()

    # Second server instance: must load session from disk.
    inp2 = WebSocketInputAdapter()
    store2 = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    store2.set_agent_pool_map(agent_pool_map)
    server2 = WebUIServer(
        inp2,
        store2,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    server2.set_workspace_index(store2)
    server2.set_agent_pool_map(agent_pool_map)
    server2.set_pool_agent_names(["main", "coding"])
    session_store2 = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server2.set_session_store(session_store2)
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


# ── Test 7: Different conversations → different pools → no cross-talk ──


@pytest.mark.asyncio
async def test_different_conversations_route_to_different_pools() -> None:
    """Two conversations, two pools — PoolRouter routes each correctly."""
    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)

    callback, real_store, calls = _make_real_callback(data_dir)
    server.set_pool_switch_callback(callback)

    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, server._store, inp, pool_session_store=real_store, workspace_root=data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        # Create coding conversation
        resp = await client.post("/api/sessions", json={"pool": "coding"})
        coding_conv = (await resp.json())["session_id"].split(".")[0]

        # Create main conversation
        resp = await client.post("/api/sessions", json={"pool": "main"})
        main_conv = (await resp.json())["session_id"].split(".")[0]

        # Send to coding conv
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "session_id": f"{coding_conv}.coding"})
        _unwrap_envelope(await ws.receive_json())
        await ws.send_json({
            "action": "send_message",
                "session_id": f"{coding_conv}.coding",
            "content": "coding msg",
        })
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["agent_name"] == "coding"

        # Send to main conv
        await ws.send_json({"action": "attach", "session_id": f"{main_conv}.main"})
        _unwrap_envelope(await ws.receive_json())
        await ws.send_json({
            "action": "send_message",
                "session_id": f"{main_conv}.main",
            "content": "main msg",
        })
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["agent_name"] == "main"

        # Verify PoolSessionStore routes correctly
        # Drain all messages from queue
        messages: list[tuple[str, str]] = []
        while not inp._message_queue.empty():
            msg = inp._message_queue.get_nowait()
            pool = real_store.get(msg.session.session_id_prefix, "main")
            messages.append((msg.session.session_id_prefix, pool))

        coding_routes = [(s, p) for s, p in messages if s == coding_conv]
        main_routes = [(s, p) for s, p in messages if s == main_conv]

        assert all(p == "coding" for _, p in coding_routes), (
            f"All coding conv messages must route to 'coding': {coding_routes}"
        )
        assert all(p == "main" for _, p in main_routes), (
            f"All main conv messages must route to 'main': {main_routes}"
        )
    finally:
        await client.close()


# ── Test 8: Control command still intercepted in multi-channel setup ──


@pytest.mark.asyncio
async def test_control_interception_in_full_server_flow() -> None:
    """When user sends /cd in WebUI, the pipeline terminates it at S6
    (SkillParseStage) and the server sends an error envelope — the
    message never reaches PoolRouter."""
    data_dir = Path(tempfile.mkdtemp())
    inp = WebSocketInputAdapter()
    holder: list = []
    def _ws_resolver() -> str:
        s = holder[0] if holder else None
        return str(s._workspace_control.current) if s is not None and s._workspace_control is not None else ""
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    holder.append(server)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main", "coding"])

    # Inject the input pipeline
    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, store, inp, workspace_root=data_dir)

    callback, real_store, calls = _make_real_callback(data_dir)
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        ws = await client.ws_connect("/ws")

        # Create a conversation
        resp = await client.post("/api/sessions", json={"pool": "main"})
        conv_id = (await resp.json())["session_id"].split(".")[0]

        await ws.send_json({"action": "attach", "session_id": f"{conv_id}.main"})
        _unwrap_envelope(await ws.receive_json())

        # ── Send /cd command ──
        q_before = inp._message_queue.qsize()
        await ws.send_json({
            "action": "send_message",
            "session_id": f"{conv_id}.main",
            "content": "/cd /tmp",
        })

        # /cd reaches S6 (unknown skill) — terminated, NOT enqueued.
        # Client receives an error envelope.
        await asyncio.sleep(0.15)
        assert inp._message_queue.qsize() == q_before, (
            f"/cd must NOT be enqueued. "
            f"Queue before={q_before}, after={inp._message_queue.qsize()}"
        )
        err = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert err["event"] == "error", (
            f"Expected error envelope for /cd, got {err.get('event')}"
        )

        # ── Send normal message — must be enqueued ──
        await ws.send_json({
            "action": "send_message",
            "session_id": f"{conv_id}.main",
            "content": "normal message",
        })
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"
        assert echoed["content"] == "normal message"

        queue_after_normal = inp._message_queue.qsize()
        assert queue_after_normal > q_before, (
            f"Normal message must be enqueued. "
            f"Queue before={q_before}, after={queue_after_normal}"
        )

    finally:
        await client.close()


# ── Test 9: Production simulation — _initialize_pool wires control filter ──


@pytest.mark.asyncio
async def test_initialize_pool_wires_control_filter_to_websocket() -> None:
    """Simulate what _initialize_pool does: configure_control_filter on input_adapter.

    The WebSocketInputAdapter (used by server for _try_intercept_control)
    MUST have _cmd_processor and _control_channel set after this call.
    """
    from bot.adapters.fan_in import FanInInputAdapter

    from framework.commands.handlers import build_default_builtin_handlers
    from framework.commands.processor import SlashCommandProcessor
    from framework.control.channel import InMemoryControlChannel

    ws_input = WebSocketInputAdapter()

    # FanIn wraps the WebSocket adapter (multi-channel setup)
    fan_in = FanInInputAdapter()
    fan_in.add_source(ws_input)

    # Build command processor as _build_main_command_processor does
    workspace_ctrl = MagicMock()
    workspace_ctrl.home = Path("/fake/home")

    handlers = list(build_default_builtin_handlers())
    processor = SlashCommandProcessor(handlers=handlers)
    channel = InMemoryControlChannel()

    # ── Critical call from _initialize_pool ──
    fan_in.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=None,
        session_checker=None,
        turn_uuid_getter=None,
    )

    # After _initialize_pool: WebSocket adapter MUST be configured
    assert ws_input._cmd_processor is not None, (
        "CRITICAL: WebSocketInputAdapter._cmd_processor must NOT be None "
        "after _initialize_pool. Without this, _try_intercept_control "
        "in _ws_send_message always returns False — terminal/commands broken."
    )
    assert ws_input._control_channel is not None, (
        "CRITICAL: WebSocketInputAdapter._control_channel must NOT be None."
    )

    # Verify /stop interception actually works (cd/exit/pwd are handled by S2 now)
    result = await ws_input._try_intercept_control("/stop", "test.main")
    assert result is True, f"/stop must be intercepted, got {result}"

    # Verify /cd is NOT intercepted by _try_intercept_control (handled by S2)
    result = await ws_input._try_intercept_control("/cd /tmp", "test.main")
    assert result is False, f"/cd must NOT be intercepted by _try_intercept_control, got {result}"

    # Normal text passes through
    result = await ws_input._try_intercept_control("hello", "test.main")
    assert result is False, f"Normal text must NOT be intercepted, got {result}"


# ── Test 10: Server._ws_send_message intercepts /cd before enqueue ──


@pytest.mark.asyncio
async def test_server_intercepts_cd_before_enqueue() -> None:
    """WebUI chat /cd reaches S6 (SkillParseStage) which terminates it
    as unknown skill — not enqueued, error envelope sent to client."""

    data_dir = Path(tempfile.mkdtemp())
    inp = WebSocketInputAdapter()
    holder: list = []
    def _ws_resolver() -> str:
        s = holder[0] if holder else None
        return str(s._workspace_control.current) if s is not None and s._workspace_control is not None else ""
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=data_dir,
        home_sessions_dir=WorkspacePaths(root=data_dir / ".modex").sessions_dir,
    )
    holder.append(server)
    server.set_workspace_index(store)
    server.set_pool_agent_names(["main"])

    # Inject the input pipeline (no-op skill registry — /cd terminates at S6)
    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, store, inp, workspace_root=data_dir)

    callback, _, _ = _make_real_callback(data_dir)
    server.set_pool_switch_callback(callback)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        ws = await client.ws_connect("/ws")
        resp = await client.post("/api/sessions", json={"pool": "main"})
        conv_id = (await resp.json())["session_id"].split(".")[0]

        await ws.send_json({"action": "attach", "session_id": f"{conv_id}.main"})
        _unwrap_envelope(await ws.receive_json())

        q_before = inp._message_queue.qsize()

        # /cd must be terminated by S6 — NOT enqueued
        await ws.send_json({
            "action": "send_message",
            "session_id": f"{conv_id}.main",
            "content": "/cd /tmp",
        })
        await asyncio.sleep(0.15)

        assert inp._message_queue.qsize() == q_before, (
            f"/cd must be terminated BEFORE enqueue. "
            f"Queue grew from {q_before} to {inp._message_queue.qsize()}"
        )
        # WebUI chat no longer handles /cd (UI does); client gets an error envelope.
        err = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert err["event"] == "error"

        # Normal text must be enqueued
        await ws.send_json({
            "action": "send_message",
            "session_id": f"{conv_id}.main",
            "content": "hello",
        })
        echoed = _unwrap_envelope(await ws.receive_json(timeout=2))
        assert echoed["event"] == "user_message"
        assert inp._message_queue.qsize() > q_before

    finally:
        await client.close()


# ── Test 11: Conversations survive pool switching (regression for sidebar-empty bug) ──


@pytest.mark.asyncio
async def test_conversations_survive_pool_switching() -> None:
    """GET /api/sessions must return ALL conversations across all pools,
    and must survive repeated calls (simulating pool dropdown switching).

    Regression test for: switching pool → sidebar empty → switching back → still empty.
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from framework.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)
    callback, real_store, calls = _make_real_callback(data_dir)
    server.set_pool_switch_callback(callback)

    agent_pool_map = {"main": "main", "coding": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)

    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, server._store, inp, pool_session_store=real_store, workspace_root=data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        ws = await client.ws_connect("/ws")

        # ── Create and message main-pool conversation ──
        resp = await client.post("/api/sessions", json={"pool": "main"})
        main_sid = (await resp.json())["session_id"]
        main_conv = main_sid.split(".")[0]
        await session_store.save(SessionInfo(
            session_id=main_sid, agent_name="main",
            created_at=now_ms(), updated_at=now_ms(),
        ))
        await ws.send_json({"action": "attach", "session_id": f"{main_conv}.main"})
        _unwrap_envelope(await ws.receive_json())
        await ws.send_json({
            "action": "send_message",
                "session_id": f"{main_conv}.main",
            "content": "main pool message",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))

        # ── Create and message coding-pool conversation ──
        resp = await client.post("/api/sessions", json={"pool": "coding"})
        coding_sid = (await resp.json())["session_id"]
        coding_conv = coding_sid.split(".")[0]
        await session_store.save(SessionInfo(
            session_id=coding_sid, agent_name="coding",
            created_at=now_ms(), updated_at=now_ms(),
        ))
        await ws.send_json({"action": "attach", "session_id": f"{coding_conv}.coding"})
        _unwrap_envelope(await ws.receive_json())
        await ws.send_json({
            "action": "send_message",
                "session_id": f"{coding_conv}.coding",
            "content": "coding pool message",
        })
        _unwrap_envelope(await ws.receive_json(timeout=2))

        # ── GET /api/sessions must return BOTH ──
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert main_conv in conv_ids, (
            f"Main pool conversation {main_conv!r} MISSING from /api/sessions"
        )
        assert coding_conv in conv_ids, (
            f"Coding pool conversation {coding_conv!r} MISSING from /api/sessions"
        )

        # ── Verify pool field is correct ──
        by_id = {s["session_id"].split(".")[0]: s for s in sessions}
        assert by_id[main_conv]["pool"] == "main"
        assert by_id[coding_conv]["pool"] == "coding"

        # ── "Switch pools" — multiple fetchSessions() calls return stable result ──
        for _ in range(3):
            resp = await client.get("/api/sessions")
            assert resp.status == 200
            sessions2 = await resp.json()
            conv_ids2 = {s["session_id"].split(".")[0] for s in sessions2}
            assert main_conv in conv_ids2, "After pool switch, main conv MISSING"
            assert coding_conv in conv_ids2, "After pool switch, coding conv MISSING"

    finally:
        await client.close()


# ── Test 12: New conversation without message survives fetchSessions() ──


@pytest.mark.asyncio
async def test_conversation_visible_after_first_message() -> None:
    """A conversation created via WS attach + send_message must appear in
    /api/sessions after the first message. Empty (no-message) sessions are
    client-side only and do NOT appear in the server session list."""
    from bot.service.session_store import WorkspacePoolSessionStore
    from framework.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)
    callback, real_store, calls = _make_real_callback(data_dir)
    server.set_pool_switch_callback(callback)

    agent_pool_map = {"main": "main", "coding": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)

    from tests.webui._pipeline_fixture import attach_default_pipeline
    attach_default_pipeline(server, server._store, inp, pool_session_store=real_store, workspace_root=data_dir)

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
      with bind_workspace_root(data_dir):
        ws = await client.ws_connect("/ws")

        coding_uuid = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": coding_uuid, "pool": "coding"})
        attached1 = _unwrap_envelope(await ws.receive_json())
        assert attached1["event"] == "attached"
        coding_sid = attached1["session_id"]

        main_uuid = _new_uuid_prefix()
        await ws.send_json({"action": "attach", "uuid_prefix": main_uuid, "pool": "main"})
        attached2 = _unwrap_envelope(await ws.receive_json())
        assert attached2["event"] == "attached"
        main_sid = attached2["session_id"]

        # Before any message, empty sessions do NOT appear in /api/sessions
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert coding_uuid not in conv_ids, "Empty sessions must not appear in /api/sessions"
        assert main_uuid not in conv_ids, "Empty sessions must not appear in /api/sessions"

        # Send first message in coding
        await ws.send_json({"action": "send_message", "session_id": coding_sid, "content": "hello coding"})
        _unwrap_envelope(await ws.receive_json(timeout=2))

        # Save coding session to the store after first message.
        await session_store.save(SessionInfo(
            session_id=coding_sid,
            agent_name="coding",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        # After first message, coding appears
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert coding_uuid in conv_ids, "Coding conversation must appear after first message"
        assert main_uuid not in conv_ids, "Main (empty) must not appear before first message"

        # Send first message in main
        await ws.send_json({"action": "send_message", "session_id": main_sid, "content": "hello main"})
        _unwrap_envelope(await ws.receive_json(timeout=2))

        # Save main session to the store after first message.
        await session_store.save(SessionInfo(
            session_id=main_sid,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

        # After first message, main also appears
        resp = await client.get("/api/sessions")
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}
        assert coding_uuid in conv_ids
        assert main_uuid in conv_ids
    finally:
        await client.close()


# ── Test 13: External adapter conversations visible in /api/sessions ──


@pytest.mark.asyncio
async def test_sessions_includes_external_adapter_conversations() -> None:
    """_handle_sessions must include conversations written to the transcript
    store by external adapters (QQ, etc.), even though those adapters never
    touch the server's _conversations cache.

    This is the root cause of: "IM conversations can't be loaded".
    """
    from bot.service.session_store import WorkspacePoolSessionStore
    from bot.webui.events import UserMessageEvent
    from framework.core.session_id import SessionInfo, now_ms

    data_dir = Path(tempfile.mkdtemp())
    server, inp = _make_server(data_dir)

    # Inject session store for session listing.
    agent_pool_map = {"main": "main", "coding": "coding"}
    session_store = WorkspacePoolSessionStore(
        base_dir=data_dir,
        pool_resolver=lambda s: agent_pool_map.get(s.agent_name, "main"),
    )
    server.set_session_store(session_store)

    # ── Simulate QQ adapter writing to the shared transcript store ──

    qq_conv_id = "qq_user_12345"
    qq_sid = f"{qq_conv_id}.main"
    event = UserMessageEvent(
        session_id=qq_sid,
        agent_name="main",
        content="QQ message",
    )
    with bind_workspace_root(data_dir):
        server._store.append(qq_sid, event)

        # Save QQ session to the session store so it appears in listing.
        await session_store.save(SessionInfo(
            session_id=qq_sid,
            agent_name="main",
            created_at=now_ms(),
            updated_at=now_ms(),
        ))

    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.get("/api/sessions")
        assert resp.status == 200
        sessions = await resp.json()
        conv_ids = {s["session_id"].split(".")[0] for s in sessions}

        assert qq_conv_id in conv_ids, (
            f"QQ conversation {qq_conv_id!r} NOT in /api/sessions. "
            f"Transcript store HAS data for it. "
            f"server._conversations={server._conversations!r}. "
            f"Fix: _handle_sessions must read from "
            f"self._store.list_conversations() as source of truth."
        )

        # Verify the QQ session has correct data
        qq_session = next(s for s in sessions if s["session_id"].split(".")[0] == qq_conv_id)
        assert qq_session["agent_name"] == "main", (
            f"QQ session should have agent 'main', got {qq_session['agent_name']}"
        )
        assert qq_session["pool"] == "main", (
            f"QQ session should be in pool 'main', got {qq_session['pool']}"
        )

    finally:
        await client.close()


# ── Test 14: PoolRouter forwards full session id so AgentPool reuses it ──


@pytest.mark.asyncio
async def test_pool_router_forwards_agent_session_id() -> None:
    """PoolRouter must include ``agent_session_id`` in the BrokerMessage it sends
    to the pool.  Without it, AgentPool's ``from_broker_message`` returns None,
    falls back to ``_dispatch_raw_broker_message``, and re-encodes the
    session_id — creating a brand-new session instead of routing to the
    existing one.
    """
    from framework.core.session_id import SessionInfo
    from framework.core.types import InputMessage
    from framework.messaging.broker import BrokerMessage

    data_dir = Path(tempfile.mkdtemp())
    session_store = PoolSessionStore(data_dir)

    # Mock broker that captures sent messages
    sent_messages: list[tuple[Any, BrokerMessage]] = []

    class _MockBroker:
        async def send_to(self, address: Any, msg: BrokerMessage) -> None:
            sent_messages.append((address, msg))

    class _MockPool:
        main_agent_name = "coding"
        main_address = "pool:coding"

    router = PoolRouter(
        input_adapter=MagicMock(),
        broker=_MockBroker(),
        pools={"coding": _MockPool()},
        session_store=session_store,
        default_pool="main",
    )

    existing_sid = "legacy123.coding"
    await router._route_to_pool(
        InputMessage(
            content="hello",
            session=SessionInfo(
                session_id=existing_sid,
                agent_name="coding",
            ),
            source="websocket",
            channel="websocket",
        ),
        _MockPool(),
    )

    assert len(sent_messages) == 1
    address, broker_msg = sent_messages[0]
    assert address == "pool:coding"
    assert broker_msg.headers.get("agent_session_id") == existing_sid, (
        f"PoolRouter must forward existing session id in headers; "
        f"got {broker_msg.headers!r}"
    )
    assert broker_msg.payload.get("agent_session_id") == existing_sid, (
        f"PoolRouter should also set agent_session_id in payload for robustness; "
        f"got {broker_msg.payload!r}"
    )
    assert broker_msg.payload.get("session_id") == "legacy123"
    assert broker_msg.headers.get("session_id") == "legacy123"
