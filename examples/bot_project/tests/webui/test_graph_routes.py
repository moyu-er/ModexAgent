"""Tests for the graph REST API (12 endpoints, T13).

Covers all endpoints with a real ``GraphOrchestrator`` backed by in-memory
stores and a ``WebUIGraphOutputAdapter`` event store. The workspace resolver
is a simple lambda returning a ``SimpleNamespace`` with the orchestrator +
event store — the handlers only access those two attributes (G7).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.graph.output_adapter import WebUIGraphOutputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

from modex_agent.orchestration import GraphOrchestrator
from modex_graph import (
    DefaultGraphState,
    EdgeSpec,
    GraphInstanceStatus,
    GraphOutput,
    GraphSpec,
    InMemoryGraphInstanceStore,
    InMemoryGraphSpecStore,
    NodeRegistry,
    NullCoordinatorFactory,
)

_BOT_PROJECT = Path(__file__).resolve().parents[2]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

_VALID_YAML = (
    "name: test-graph\n"
    "state_class: default\n"
    "nodes: []\n"
    "edges:\n"
    "  - source: __start__\n"
    "    target: __end__\n"
    "version: '1.0'\n"
)


def _make_orchestrator() -> tuple[
    GraphOrchestrator, dict[int, list[GraphOutput]], InMemoryGraphSpecStore
]:
    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    event_store: dict[int, list[GraphOutput]] = {}
    orchestrator = GraphOrchestrator(
        node_registry=NodeRegistry(),
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=NullCoordinatorFactory(),
        output_adapter=WebUIGraphOutputAdapter(event_store),
    )
    return orchestrator, event_store, spec_store


def _save_spec(spec_store: InMemoryGraphSpecStore) -> int:
    spec = GraphSpec(
        name="test-graph",
        state_class="default",
        edges=[EdgeSpec(source="__start__", target="__end__")],
    )
    return spec_store.save(spec)


def _make_client(
    orchestrator: GraphOrchestrator,
    event_store: dict[int, list[GraphOutput]],
    tmp_path: Path,
) -> TestClient:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    resources = SimpleNamespace(
        graph_orchestrator=orchestrator,
        graph_event_store=event_store,
        target=tmp_path,
    )
    server.set_graph_workspace_resolver(lambda ws: resources)  # type: ignore[arg-type]
    return TestClient(TestServer(server.app))


# ── 503 degradation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_503_when_resolver_not_configured(tmp_path: Path) -> None:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        assert (await client.get("/api/graphs/specs")).status == 503
        assert (await client.get("/api/graphs/instances/1")).status == 503
    finally:
        await client.close()


# ── Spec endpoints ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_specs_returns_summaries(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/specs")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert len(data["specs"]) == 1
        assert data["specs"][0]["spec_id"] == str(spec_id)
        assert data["specs"][0]["name"] == "test-graph"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_spec_returns_yaml_content(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/specs/{spec_id}")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["spec_id"] == str(spec_id)
        assert data["name"] == "test-graph"
        assert "state_class" in data["yaml_content"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_spec_404_when_not_found(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/specs/999999")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_spec_validates_and_saves(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.put(
            f"/api/graphs/specs/{spec_id}",
            json={"yaml_content": _VALID_YAML},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["name"] == "test-graph"
        assert "state_class" in data["yaml_content"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_spec_404_when_not_found(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.put(
            "/api/graphs/specs/999999",
            json={"yaml_content": _VALID_YAML},
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_spec_400_on_name_version_mismatch(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        renamed_yaml = _VALID_YAML.replace("test-graph", "renamed")
        resp = await client.put(
            f"/api/graphs/specs/{spec_id}",
            json={"yaml_content": renamed_yaml},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "immutable" in data["error"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_spec_400_on_invalid_yaml(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.put(
            f"/api/graphs/specs/{spec_id}",
            json={"yaml_content": "name: test\n: bad yaml\n  broken"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert "error" in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_spec_400_on_missing_state_class(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        bad_yaml = (
            "name: test-graph\nedges:\n  - source: __start__\n    target: __end__\nversion: '1.0'\n"
        )
        resp = await client.put(
            f"/api/graphs/specs/{spec_id}",
            json={"yaml_content": bad_yaml},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_spec_yaml_returns_text(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/specs/{spec_id}/yaml")
        assert resp.status == 200, await resp.text()
        text = await resp.text()
        assert "state_class" in text
        assert "test-graph" in text
    finally:
        await client.close()


# ── Run endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_spec_creates_instance(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(f"/api/graphs/specs/{spec_id}/run", json={})
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["status"] == "pending"
        assert isinstance(data["graph_instance_id"], str)
    finally:
        await orch.cleanup()
        await client.close()


@pytest.mark.asyncio
async def test_run_spec_404_when_spec_missing(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post("/api/graphs/specs/999999/run", json={})
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_run_spec_with_user_input(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(
            f"/api/graphs/specs/{spec_id}/run",
            json={"user_input": {"content": "hello"}},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["status"] == "pending"
    finally:
        await orch.cleanup()
        await client.close()


# ── Instance query endpoints ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_instance_returns_state(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/instances/{gid}")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["graph_instance_id"] == str(gid)
        assert data["status"] == "pending"
        assert isinstance(data["nodes"], list)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_instance_404_when_not_found(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/instances/999999")
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_instances_returns_all(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid1 = await orch.create_instance(spec_id)
    gid2 = await orch.create_instance(spec_id)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/instances")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        ids = {item["graph_instance_id"] for item in data}
        assert str(gid1) in ids
        assert str(gid2) in ids
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_instances_with_status_filter(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    await orch.create_instance(spec_id)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/instances?status=pending")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert len(data) >= 1
        assert all(item["status"] == "pending" for item in data)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_list_instances_400_on_invalid_status(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/instances?status=bogus")
        assert resp.status == 400
    finally:
        await client.close()


# ── Events endpoint ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_returns_outputs(tmp_path: Path) -> None:
    orch, event_store, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_and_run(spec_id)
    client = _make_client(orch, event_store, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/instances/{gid}/events")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert len(data["events"]) >= 1
        assert data["events"][0]["graph_instance_id"] == str(gid)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_events_empty_when_no_events(tmp_path: Path) -> None:
    orch, event_store, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    client = _make_client(orch, event_store, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/instances/{gid}/events")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["events"] == []
    finally:
        await client.close()


# ── Control endpoints ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_instance(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    orch._instance_store.update_status(gid, GraphInstanceStatus.RUNNING)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(f"/api/graphs/instances/{gid}/pause")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["status"] == "paused"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_resume_instance(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    orch._instance_store.update_status(gid, GraphInstanceStatus.PAUSED)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(f"/api/graphs/instances/{gid}/resume")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["status"] == "running"
    finally:
        await orch.cleanup()
        await client.close()


@pytest.mark.asyncio
async def test_stop_instance(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    orch._instance_store.update_status(gid, GraphInstanceStatus.RUNNING)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(f"/api/graphs/instances/{gid}/stop")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["status"] == "stopped"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_control_404_when_instance_missing(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        assert (await client.post("/api/graphs/instances/999999/pause")).status == 404
        assert (await client.post("/api/graphs/instances/999999/stop")).status == 404
    finally:
        await client.close()


# ── Deliver endpoint ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_to_node(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    # Set to RUNNING so deliver is accepted (P0-5: PENDING rejected)
    from modex_graph import GraphInstanceStatus
    orch._instance_store.update_status(gid, GraphInstanceStatus.RUNNING)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(
            f"/api/graphs/instances/{gid}/deliver",
            json={"node_name": "__end__", "content": {"content": "hello"}},
        )
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["status"] == "delivered"
        assert data["node_name"] == "__end__"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_deliver_400_on_missing_fields(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.post(
            f"/api/graphs/instances/{gid}/deliver",
            json={"node_name": "x"},
        )
        assert resp.status == 400
    finally:
        await client.close()


# ── Workspace ID extraction (G7) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workspace_id_from_header(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    _save_spec(spec_store)
    received_ids: list[str] = []

    def resolver(ws_id: str) -> SimpleNamespace:
        received_ids.append(ws_id)
        return SimpleNamespace(graph_orchestrator=orch, graph_event_store={})

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_graph_workspace_resolver(lambda ws: resolver(ws))  # type: ignore[arg-type]
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        await client.get("/api/graphs/specs", headers={"X-Workspace-Id": "ws-from-header"})
        assert received_ids == ["ws-from-header"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_workspace_id_from_query_param(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    _save_spec(spec_store)
    received_ids: list[str] = []

    def resolver(ws_id: str) -> SimpleNamespace:
        received_ids.append(ws_id)
        return SimpleNamespace(graph_orchestrator=orch, graph_event_store={})

    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_graph_workspace_resolver(lambda ws: resolver(ws))  # type: ignore[arg-type]
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        await client.get("/api/graphs/specs?ws=ws-from-query")
        assert received_ids == ["ws-from-query"]
    finally:
        await client.close()


# ── P1: Workspace parameter convergence + evicted resume ─────────────────────


@pytest.mark.asyncio
async def test_p1_1_graph_routes_accept_ws_query_param(tmp_path: Path) -> None:
    """P1-1: graph routes must accept ``?ws=`` (the global WebUI convention),
    not ``?workspace=`` (the divergent param graph routes used).
    """
    received_ids: list[str] = []

    def resolver(ws_id: str) -> Any:
        received_ids.append(ws_id)
        return resources

    orch, event_store, spec_store = _make_orchestrator()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    resources = SimpleNamespace(
        graph_orchestrator=orch,
        graph_event_store=event_store,
        target=tmp_path,
    )
    server.set_graph_workspace_resolver(lambda ws: resolver(ws))  # type: ignore[arg-type]
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # The global convention is ?ws= — graph routes must follow it.
        resp = await client.get("/api/graphs/specs?ws=ws-from-query")
        assert resp.status == 200
        assert received_ids == ["ws-from-query"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_p1_3_resume_evicted_instance_via_recovery_path(
    tmp_path: Path,
) -> None:
    """P1-3: resuming a PAUSED instance that was evicted from
    ``_active_instances`` (simulating bot restart) must go through the
    recovery path (``_run_existing_instance``), not ``start_run``.
    """
    from modex_graph import (
        DefaultGraphState,
        EdgeSpec,
        GraphInstanceStatus,
        GraphSpec,
        InMemoryGraphInstanceStore,
        InMemoryGraphSpecStore,
        NodeRegistry,
        NullCoordinatorFactory,
        NodeSpec,
    )
    from modex_graph.nodes.human_input_node import HumanInputNode, HumanInputNodeFactory
    from modex_agent.orchestration import GraphOrchestrator
    import asyncio

    node_registry = NodeRegistry()
    node_registry.register("human_input", HumanInputNodeFactory())
    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    orch = GraphOrchestrator(
        node_registry=node_registry,
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=NullCoordinatorFactory(),
    )
    spec = GraphSpec(
        name="human_input_graph",
        nodes=[NodeSpec(name="entry", node_type="human_input")],
        edges=[
            EdgeSpec(source="__start__", target="entry"),
            EdgeSpec(source="entry", target="__end__"),
        ],
        state_class="default",
    )
    spec_id = spec_store.save(spec)
    gid = await orch.create_instance(spec_id)

    # Run — HumanInputNode will GraphInterrupt → PAUSED
    from modex_graph.exceptions import GraphInterrupt as _GraphInterrupt

    with pytest.raises(_GraphInterrupt):
        await orch.run_instance(gid)

    meta = instance_store.load(gid)
    assert meta is not None
    assert meta.status is GraphInstanceStatus.PAUSED
    assert gid in orch._active_instances  # type: ignore[attr-defined]

    # Simulate eviction (bot restart): instance gone from _active_instances
    orch._active_instances.clear()  # type: ignore[attr-defined]
    orch._running_gids.clear()  # type: ignore[attr-defined]

    # Resume must go through recovery path (start_resume → _run_existing_instance)
    orch.start_resume(gid)  # type: ignore[attr-defined]
    await asyncio.sleep(0.3)

    # Instance should be back in _active_instances (recovery rebuilt it)
    assert gid in orch._active_instances  # type: ignore[attr-defined]


# ── G12: Topology endpoint (§11.3) ─────────────────────────────────────────────


def _save_spec_with_nodes(spec_store: InMemoryGraphSpecStore) -> int:
    """Save a spec with real functional nodes for topology/result tests."""
    from modex_graph import NodeSpec, NodeTrigger

    spec = GraphSpec(
        name="topo-graph",
        state_class="default",
        scheduler="parallel",
        default_trigger=NodeTrigger.ON_RECEIVE,
        nodes=[
            NodeSpec(
                name="designer",
                node_type="agent",
                config={"agent": "design", "pool": "review"},
            ),
            NodeSpec(
                name="reviewer",
                node_type="agent",
                config={"agent": "review"},
                trigger=NodeTrigger.ON_ALL_PREDS,
            ),
        ],
        edges=[
            EdgeSpec(source="__start__", target="designer"),
            EdgeSpec(source="designer", target="reviewer"),
            EdgeSpec(source="reviewer", target="designer"),  # loop back
            EdgeSpec(source="reviewer", target="__end__"),
        ],
    )
    return spec_store.save(spec)


@pytest.mark.asyncio
async def test_get_topology_returns_structure(tmp_path: Path) -> None:
    orch, _, spec_store = _make_orchestrator()
    spec_id = _save_spec_with_nodes(spec_store)
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/specs/{spec_id}/topology")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["spec_id"] == str(spec_id)
        assert data["name"] == "topo-graph"
        assert data["scheduler"] == "parallel"
        assert data["default_trigger"] == "on_receive"
        assert data["entry_node"] == "__start__"
        # 2 declared functional nodes
        node_names = [n["name"] for n in data["nodes"]]
        assert node_names == ["designer", "reviewer"]
        # designer node
        designer = data["nodes"][0]
        assert designer["node_type"] == "agent"
        assert designer["config"]["agent"] == "design"
        assert designer["config"]["pool"] == "review"
        assert designer["trigger"] is None  # uses graph default
        # reviewer node has per-node trigger override
        reviewer = data["nodes"][1]
        assert reviewer["trigger"] == "on_all_preds"
        # edges include loop back
        edge_pairs = [(e["source"], e["target"]) for e in data["edges"]]
        assert ("__start__", "designer") in edge_pairs
        assert ("reviewer", "designer") in edge_pairs  # loop back
        assert ("reviewer", "__end__") in edge_pairs
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_topology_404_when_not_found(tmp_path: Path) -> None:
    orch, _, _ = _make_orchestrator()
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/graphs/specs/999999/topology")
        assert resp.status == 404
    finally:
        await client.close()


# ── G12: Node result (§11.4) ──────────────────────────────────────────────────


def _make_orchestrator_with_inmemory_state() -> tuple[
    GraphOrchestrator, dict[int, list[GraphOutput]], InMemoryGraphSpecStore
]:
    """Build an orchestrator backed by InMemoryNodeStateStore so that
    NodeInvocationRecords survive after instance eviction.

    The factory caches the InMemoryNodeStateStore per graph_instance_id so
    that the recovery path (``get_state`` for evicted instances) sees the
    same store that was populated during the run.
    """
    from modex_graph import (
        DefaultGraphState,
        InMemoryDeliverStoreFactory,
        InMemoryNodeStateStore,
        NodeRegistry,
    )
    from modex_graph.persistence.persistence_coordinator import (
        CoordinatorFactory,
        GraphPersistenceCoordinator,
    )

    spec_store = InMemoryGraphSpecStore()
    instance_store = InMemoryGraphInstanceStore()
    event_store: dict[int, list[GraphOutput]] = {}
    _state_stores: dict[int, InMemoryNodeStateStore] = {}

    class _InMemoryCoordinatorFactory(CoordinatorFactory):
        def create(self, graph_instance_id, instance_store):
            store = _state_stores.get(graph_instance_id)
            if store is None:
                store = InMemoryNodeStateStore(graph_instance_id)
                _state_stores[graph_instance_id] = store
            return GraphPersistenceCoordinator(
                graph_instance_id=graph_instance_id,
                instance_store=instance_store,
                node_state_store=store,
                default_deliver_store_factory=InMemoryDeliverStoreFactory(),
            )

    orchestrator = GraphOrchestrator(
        node_registry=NodeRegistry(),
        state_classes={"default": DefaultGraphState},
        spec_store=spec_store,
        instance_store=instance_store,
        coordinator_factory=_InMemoryCoordinatorFactory(),
        output_adapter=WebUIGraphOutputAdapter(event_store),
    )
    return orchestrator, event_store, spec_store


@pytest.mark.asyncio
async def test_get_instance_node_result_completed(tmp_path: Path) -> None:
    orch, event_store, spec_store = _make_orchestrator_with_inmemory_state()
    spec_id = _save_spec(spec_store)  # __start__ → __end__
    gid = await orch.create_and_run(spec_id)
    client = _make_client(orch, event_store, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/instances/{gid}")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        # At least the __end__ node should be completed with a result
        # (END node collects upstream delivers into state.result)
        end_node = next(
            (n for n in data["nodes"] if n["node_name"] == "__end__"), None
        )
        assert end_node is not None, "expected __end__ node in response"
        assert end_node["status"] == "completed"
        assert end_node["result"] is not None
        assert "content" in end_node["result"]
    finally:
        await orch.cleanup()
        await client.close()


@pytest.mark.asyncio
async def test_get_instance_node_result_none_for_pending(tmp_path: Path) -> None:
    """Non-completed nodes should have result=None."""
    orch, _, spec_store = _make_orchestrator_with_inmemory_state()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)  # not run — stays pending
    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/instances/{gid}")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        for node in data["nodes"]:
            assert node["result"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_instance_node_result_truncation(tmp_path: Path) -> None:
    """Long result content must be truncated to 500 chars + '...' suffix."""
    from modex_graph import InvocationStatus, NodeInvocationRecord

    orch, _, spec_store = _make_orchestrator_with_inmemory_state()
    spec_id = _save_spec(spec_store)
    gid = await orch.create_instance(spec_id)

    # Access the live coordinator (instance not evicted — no run)
    instance = orch._active_instances.get(gid)
    assert instance is not None
    coordinator = instance.coordinator
    # Find the __end__ node_id from metadata
    metadata = orch._instance_store.load(gid)
    assert metadata is not None
    end_id = metadata.node_id_map.get("__end__")
    assert end_id is not None

    # Inject a completed invocation with a very long result
    long_content = "x" * 1000
    fake_record = NodeInvocationRecord(
        invocation_id=1,
        graph_instance_id=gid,
        node_id=end_id,
        version=1,
        parent_version=None,
        status=InvocationStatus.COMPLETED,
        state_json={"result": [{"content": long_content}]},
        suspended=False,
        created_at=0,
        updated_at=0,
    )
    coordinator._node_state_store._records[end_id] = [fake_record]

    client = _make_client(orch, {}, tmp_path)
    await client.start_server()
    try:
        resp = await client.get(f"/api/graphs/instances/{gid}")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        end_node = next(
            (n for n in data["nodes"] if n["node_name"] == "__end__"), None
        )
        assert end_node is not None
        assert end_node["result"] is not None
        content = end_node["result"]["content"]
        assert len(content) == 503  # 500 + "..."
        assert content.endswith("...")
    finally:
        await client.close()
