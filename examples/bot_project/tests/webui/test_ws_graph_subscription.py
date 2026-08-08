"""Tests for the WS graph event subscription protocol (G10, PRD §11.2).

Covers the ``subscribe_graph`` / ``unsubscribe_graph`` actions, the
``WebUIGraphOutputAdapter`` dual channel (event store + subscriber fan-out),
and the subscription lifecycle (unsubscribe / disconnect cleanup, instance
and connection isolation, attach not clearing graph subscriptions).

Server setup mirrors ``test_ws_partitioning_convergence.py`` (full session
wiring) plus a graph workspace resolver returning a ``SimpleNamespace`` with
the graph resources, mirroring ``test_graph_routes.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import ClientWebSocketResponse
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.graph.output_adapter import WebUIGraphOutputAdapter
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

from modex_agent.core.session_id import SessionIdFactory
from modex_agent.orchestration import GraphOrchestrator
from modex_agent.workspace.paths import WorkspacePaths
from modex_graph import (
    DefaultGraphState,
    GraphOutput,
    GraphOutputKind,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    NodeRegistry,
    NullCoordinatorFactory,
)

_BOT_PROJECT = Path(__file__).resolve().parents[2]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

_DATA_DIR_NAME = ".modex"

# Snowflake-scale instance ids (> 2**53) to prove str serialization on the wire.
_GID_A = 7300000000000000001
_GID_B = 7300000000000000002


def _build_server(home: Path, resources: SimpleNamespace) -> WebUIServer:
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=_DATA_DIR_NAME)
    store.set_agent_pool_map({"main": "main"})
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        data_dir=home,
        home_sessions_dir=WorkspacePaths(root=home / _DATA_DIR_NAME).sessions_dir,
    )
    server.set_workspace_index(store)
    server.set_data_dir_name(_DATA_DIR_NAME)
    server.set_agent_pool_map({"main": "main"})
    server.set_pool_agent_names(["main"])
    server.set_session_factory(SessionIdFactory())
    server.set_session_store(
        WorkspacePoolSessionStore(base_dir=home, pool_resolver=lambda s: "main")
    )
    server.set_graph_workspace_resolver(lambda ws: resources)
    return server


def _make_resources(tmp_path: Path) -> tuple[SimpleNamespace, WebUIGraphOutputAdapter]:
    """Workspace graph resources: real orchestrator + store + subscriber registry."""
    event_store: dict[int, list[GraphOutput]] = {}
    subscribers: dict[int, list[asyncio.Queue[GraphOutput]]] = {}
    adapter = WebUIGraphOutputAdapter(event_store, subscribers)
    orchestrator = GraphOrchestrator(
        node_registry=NodeRegistry(),
        state_classes={"default": DefaultGraphState},
        spec_store=InMemoryGraphSpecStore(),
        instance_store=InMemoryGraphInstanceStore(),
        coordinator_factory=NullCoordinatorFactory(),
        output_adapter=adapter,
    )
    resources = SimpleNamespace(
        graph_orchestrator=orchestrator,
        graph_event_store=event_store,
        graph_event_subscribers=subscribers,
        target=tmp_path,
    )
    return resources, adapter


def _node_started(gid: int) -> GraphOutput:
    return GraphOutput(
        kind=GraphOutputKind.NODE_STARTED,
        graph_instance_id=gid,
        node_id="node-1",
        node_name="designer",
        invocation_id=1,
        timestamp=1733000000000,
    )


async def _subscribe(ws: ClientWebSocketResponse, gid: int) -> dict[str, Any]:
    await ws.send_json({"action": "subscribe_graph", "instance_id": str(gid), "ws": ""})
    ack = await ws.receive_json(timeout=2)
    assert ack["type"] == "graph_subscribed"
    assert ack["graph_instance_id"] == str(gid)
    assert isinstance(ack["graph_instance_id"], str)
    return ack


async def _assert_no_message(ws: ClientWebSocketResponse, timeout: float = 0.3) -> None:
    try:
        leaked = await ws.receive_json(timeout=timeout)
    except TimeoutError:
        return
    raise AssertionError(f"expected no message, got {leaked}")


@pytest.mark.asyncio
async def test_subscribe_receives_graph_event(tmp_path: Path) -> None:
    """Subscribed client receives graph_event in real time, fields complete,
    instance id serialized as str; the event store gets the same event."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await _subscribe(ws, _GID_A)

        await adapter.emit(_node_started(_GID_A))

        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "graph_event"
        assert msg["graph_instance_id"] == str(_GID_A)
        assert isinstance(msg["graph_instance_id"], str)
        event = msg["event"]
        assert event["kind"] == "node_started"
        assert event["graph_instance_id"] == _GID_A
        assert event["node_id"] == "node-1"
        assert event["node_name"] == "designer"
        assert event["invocation_id"] == 1
        assert event["timestamp"] == 1733000000000

        # Dual channel, same source: the REST event store has the same event.
        assert resources.graph_event_store[_GID_A] == [_node_started(_GID_A)]
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(tmp_path: Path) -> None:
    """After unsubscribe_graph the queue is deregistered and no further
    events are pushed to the client."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await _subscribe(ws, _GID_A)

        await adapter.emit(_node_started(_GID_A))
        assert (await ws.receive_json(timeout=2))["type"] == "graph_event"

        await ws.send_json(
            {"action": "unsubscribe_graph", "instance_id": str(_GID_A), "ws": ""}
        )
        ack = await ws.receive_json(timeout=2)
        assert ack["type"] == "graph_unsubscribed"
        assert ack["graph_instance_id"] == str(_GID_A)
        # Queue deregistered from the workspace registry.
        assert not resources.graph_event_subscribers[_GID_A]

        await adapter.emit(_node_started(_GID_A))
        await _assert_no_message(ws)
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disconnect_cleans_up_subscriptions(tmp_path: Path) -> None:
    """Closing the connection removes the subscriber queue (no leak, no
    fan-out to a dead connection)."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await _subscribe(ws, _GID_A)
        assert len(resources.graph_event_subscribers[_GID_A]) == 1

        await ws.close()
        # Server-side cleanup runs in the WS handler's finally; poll for it.
        for _ in range(100):
            if not resources.graph_event_subscribers[_GID_A]:
                break
            await asyncio.sleep(0.05)
        assert not resources.graph_event_subscribers[_GID_A], (
            "disconnect must deregister the subscriber queue"
        )

        # Emit after disconnect: no subscribers -> no-op, must not raise.
        await adapter.emit(_node_started(_GID_A))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_instance_and_connection_isolation(tmp_path: Path) -> None:
    """Each connection receives only events for the instance it subscribed
    to; an unsubscribed instance is never pushed."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws_a = await client.ws_connect("/ws")
        ws_b = await client.ws_connect("/ws")
        await _subscribe(ws_a, _GID_A)
        await _subscribe(ws_b, _GID_B)

        await adapter.emit(_node_started(_GID_A))
        await adapter.emit(_node_started(_GID_B))

        msg_a = await ws_a.receive_json(timeout=2)
        assert msg_a["type"] == "graph_event"
        assert msg_a["graph_instance_id"] == str(_GID_A)
        msg_b = await ws_b.receive_json(timeout=2)
        assert msg_b["type"] == "graph_event"
        assert msg_b["graph_instance_id"] == str(_GID_B)

        # Neither connection receives the other instance's events.
        await _assert_no_message(ws_a)
        await _assert_no_message(ws_b)
        await ws_a.close()
        await ws_b.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unsubscribed_instance_not_pushed(tmp_path: Path) -> None:
    """A connected client with NO subscription receives nothing when events
    are emitted for any instance."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await adapter.emit(_node_started(_GID_A))
        await _assert_no_message(ws)
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rest_events_endpoint_unchanged(tmp_path: Path) -> None:
    """REST polling keeps working with subscribers attached (both channels
    read from the same emit)."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await _subscribe(ws, _GID_A)

        await adapter.emit(_node_started(_GID_A))
        assert (await ws.receive_json(timeout=2))["type"] == "graph_event"

        resp = await client.get(f"/api/graphs/instances/{_GID_A}/events")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert len(data["events"]) == 1
        assert data["events"][0]["graph_instance_id"] == str(_GID_A)
        assert data["events"][0]["kind"] == "node_started"
        assert data["events"][0]["node_id"] == "node-1"
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_attach_does_not_clear_graph_subscription(tmp_path: Path) -> None:
    """Attaching (or switching) a conversation must NOT clear graph
    subscriptions -- the two lifecycles are orthogonal."""
    home = tmp_path / "home"
    home.mkdir()
    resources, adapter = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await _subscribe(ws, _GID_A)

        await ws.send_json(
            {"action": "attach", "uuid_prefix": "convG", "pool": "main", "ws": ""}
        )
        attached = await ws.receive_json(timeout=2)
        assert attached["event_type"] == "attached"
        # Subscription survived the attach.
        assert len(resources.graph_event_subscribers[_GID_A]) == 1

        await adapter.emit(_node_started(_GID_A))
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "graph_event"
        assert msg["graph_instance_id"] == str(_GID_A)
        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_subscribe_invalid_instance_id_returns_error(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    resources, _ = _make_resources(tmp_path)
    server = _build_server(home, resources)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "subscribe_graph", "instance_id": "not-an-int"})
        msg = await ws.receive_json(timeout=2)
        assert msg["type"] == "graph_error"
        assert "instance_id" in msg["message"]
        await ws.close()
    finally:
        await client.close()
