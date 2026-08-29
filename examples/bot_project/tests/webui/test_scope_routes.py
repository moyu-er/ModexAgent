"""Tests for the scope declaration REST API (ticket 16).

Covers the four endpoints with a real ``WebUIServer`` whose workspace
resolver returns a ``SimpleNamespace`` carrying ``target`` + ``ctx`` + the
real ``ComponentRegistry`` used by scope compilation. The declaration file
lives at ``<target>/config/scopes/bot.yml``; tests write it directly to disk
so the no-cache assertion (SPEC §3.4: the bill recomputes from the YAML per
request) is exercised against real file state.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from pydantic import BaseModel, JsonValue

from modex_agent.plugins.abc import PluginSource
from modex_agent.plugins.assembly.context import AgentContext as AssemblyAgentContext
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilityWiring,
    PromptSectionSpec,
    TreePositionView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext

_BOT_PROJECT = Path(__file__).resolve().parents[2]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

type JsonObject = dict[str, JsonValue]

_WORKSPACE_DECLARATION = """\
workspace:
  name: bot
  pools:
    main:
      peers:
      - helper
      agents:
        main:
          description: Root of the main pool.
          max_steps: 50
          capabilities:
            todo: {}
            experience: {}
          agents:
            worker:
              description: Child agent.
              max_steps: 60
    helper:
      peers:
      - main
      agents:
        helper:
          description: Standalone root.
"""

_POOL_ROOT_DECLARATION = """\
pool:
  name: solo
  agents:
    solo:
      description: Pool-as-root declaration.
      max_steps: 10
"""

_CAPABILITY_DECLARATION = """\
pool:
  name: capability
  agents:
    root:
      capabilities:
        aci: {}
"""

_UNKNOWN_CAPABILITY_DECLARATION = """\
pool:
  name: capability
  agents:
    root:
      capabilities:
        unregistered: {}
"""

_AUTO_CAPABILITY_DECLARATION = """\
pool:
  name: capability
  agents:
    root:
      use_terminal: true
"""

_TWO_ROOTS_DECLARATION = """\
pool:
  name: broken
  agents:
    a:
      max_steps: 10
    b:
      max_steps: 10
"""


class _ThirdPartyAutoCapability(Capability):
    name = "third_party_auto"

    def applies(self, view: AgentDeclarationView) -> bool:
        return view.declared.use_terminal is True

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        return CapabilityContribution(
            tools=("third_party_tool",),
            hooks=("third_party_hook",),
            sections=(PromptSectionSpec(section_id="third_party_auto.section", order=10),),
        )

    async def assemble(
        self, binding: CapabilityBinding, ctx: AssemblyAgentContext
    ) -> CapabilityWiring:
        return CapabilityWiring()


def _write_declaration(root: Path, text: str) -> Path:
    path = root / "config" / "scopes" / "bot.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_server(tmp_path: Path) -> WebUIServer:
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    return WebUIServer(
        WebSocketInputAdapter(),
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )


def _component_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    registration = PluginRegistrationContext(registry)
    DefaultPlugin().register(registration)
    registration.flush()
    project_registration = PluginRegistrationContext(registry, source=PluginSource.PROJECT)
    project_registration.register_capability("third_party_auto", _ThirdPartyAutoCapability())
    project_registration.flush()
    return registry


def _make_client(tmp_path: Path) -> TestClient:
    server = _make_server(tmp_path)
    resources = SimpleNamespace(
        target=tmp_path,
        ctx=WorkspaceContext.from_target(tmp_path, data_dir_name=".modex", home=tmp_path),
        component_registry=_component_registry(),
    )
    server.set_graph_workspace_resolver(lambda ws: resources)  # type: ignore[arg-type]
    return TestClient(TestServer(server.app))


def _field(bill_agent: JsonObject, name: str) -> JsonObject:
    fields = bill_agent["fields"]
    assert isinstance(fields, list)
    for field in fields:
        assert isinstance(field, dict)
        if field.get("field") == name:
            return field
    raise AssertionError(f"bill field {name!r} not found")


def _agent(bill: JsonObject, pool: str, agent: str) -> JsonObject:
    agents = bill["agents"]
    assert isinstance(agents, list)
    for entry in agents:
        assert isinstance(entry, dict)
        if entry.get("pool") == pool and entry.get("agent") == agent:
            return entry
    raise AssertionError(f"bill agent {pool}/{agent} not found")


def _tools(bill_agent: JsonObject) -> dict[str, JsonObject]:
    entries = bill_agent["tools"]
    assert isinstance(entries, list)
    result: dict[str, JsonObject] = {}
    for entry in entries:
        assert isinstance(entry, dict)
        name = entry.get("tool")
        assert isinstance(name, str)
        result[name] = entry
    return result


@pytest.mark.asyncio
async def test_503_when_resolver_not_configured(tmp_path: Path) -> None:
    client = TestClient(TestServer(_make_server(tmp_path).app))
    await client.start_server()
    try:
        assert (await client.get("/api/scope/declaration")).status == 503
        assert (await client.get("/api/scope/topology")).status == 503
        assert (await client.get("/api/scope/bill")).status == 503
        resp = await client.put("/api/scope/declaration", json={"yaml": "pool: {}"})
        assert resp.status == 503
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_404_when_declaration_missing(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        assert (await client.get("/api/scope/declaration")).status == 404
        assert (await client.get("/api/scope/topology")).status == 404
        assert (await client.get("/api/scope/bill")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_declaration_returns_raw_yaml(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/declaration")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["yaml"] == _WORKSPACE_DECLARATION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_topology_workspace_form(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/topology")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["kind"] == "workspace"
        assert data["workspace"] == "bot"
        pools = {p["name"]: p for p in data["pools"]}
        assert set(pools) == {"main", "helper"}
        assert pools["main"]["peers"] == ["helper"]
        agents = {a["name"]: a for a in pools["main"]["agents"]}
        assert agents["main"] == {"name": "main", "parent": None, "root": True}
        assert agents["worker"] == {"name": "worker", "parent": "main", "root": False}
        helper_agents = pools["helper"]["agents"]
        assert helper_agents == [{"name": "helper", "parent": None, "root": True}]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_topology_pool_as_root_no_special_casing(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/topology")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["kind"] == "pool"
        assert data["workspace"] is None
        assert data["pools"] == [
            {
                "name": "solo",
                "peers": [],
                "agents": [{"name": "solo", "parent": None, "root": True}],
            }
        ]
        # The bill path works for pool-as-root too (no workspace layer).
        bill = await client.get("/api/scope/bill")
        assert bill.status == 200, await bill.text()
        solo = _agent(await bill.json(), "solo", "solo")
        assert solo["root"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_field_layers_and_values(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/bill")
        assert resp.status == 200, await resp.text()
        bill = await resp.json()

        main = _agent(bill, "main", "main")
        assert main["root"] is True
        # Declared locally → local layer with the declared value.
        assert _field(main, "max_steps") == {
            "field": "max_steps",
            "value": 50,
            "layer": "local",
            "profile": None,
        }
        # Declared override map → local layer; the value is the effective
        # capability set in registry-enumeration order (todo + experience
        # declared, subagents auto-applied via children/peers).
        assert _field(main, "capabilities") == {
            "field": "capabilities",
            "value": ["experience", "subagents", "todo"],
            "layer": "local",
            "profile": None,
        }
        # Position-default hook rows + capability contributions, every
        # roster entry sourced (SPEC §14.8, T23).
        assert _field(main, "hooks") == {
            "field": "hooks",
            "value": [
                "deliver_retry",
                "length_guard",
                "native_env",
                "loop_detection",
                "experience_review",
                "todo_continuation",
                "todo_reorientation",
            ],
            "layer": "framework",
            "profile": None,
        }
        hook_rows = {row["hook"]: row for row in main["hooks"]}
        assert hook_rows["deliver_retry"] == {
            "hook": "deliver_retry",
            "origin": "position_default",
            "capability": None,
        }
        assert hook_rows["todo_continuation"] == {
            "hook": "todo_continuation",
            "origin": "capability_derived",
            "capability": "todo",
        }
        # Position-derived framework defaults (root → full toolset, eager).
        assert _field(main, "toolset") == {
            "field": "toolset",
            "value": "full",
            "layer": "framework",
            "profile": None,
        }
        assert _field(main, "eager")["value"] == "eager"
        assert _field(main, "eager")["layer"] == "framework"
        assert _field(main, "memory")["layer"] == "framework"

        worker = _agent(bill, "main", "worker")
        assert worker["root"] is False
        assert _field(worker, "max_steps")["value"] == 60
        # Non-root position default → read_write / lazy, framework layer.
        assert _field(worker, "toolset")["value"] == "read_write"
        assert _field(worker, "toolset")["layer"] == "framework"
        assert _field(worker, "eager")["value"] == "lazy"
        # No override declared → framework layer; only the auto-applied
        # subagents capability (non-root position).
        assert _field(worker, "capabilities") == {
            "field": "capabilities",
            "value": ["subagents"],
            "layer": "framework",
            "profile": None,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_component_implementation_sources(tmp_path: Path) -> None:
    """The O2/O3 audit surface: per-tool origins + replacement records
    (SPEC §3.5). The ``todo`` capability's contributed tools carry the
    capability-derived wire face; the dedicated aci test below covers
    capability-contributed replacements."""
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/bill")
        assert resp.status == 200, await resp.text()
        main = _agent(await resp.json(), "main", "main")

        assert main["replacements"] == []
        tools = _tools(main)
        assert tools["todo_read"]["origin"] == "capability_derived"
        assert tools["todo_read"]["capability"] == "todo"
        assert tools["todo_write"]["origin"] == "capability_derived"
        assert tools["todo_write"]["capability"] == "todo"
        assert tools["edit"]["origin"] == "preset"
        assert tools["read"]["origin"] == "preset"
        # Derived communication entries with their targets.
        assert tools["task"]["origin"] == "derived_task"
        assert tools["task"]["targets"] == ["worker"]
        assert tools["send_to_peer"]["origin"] == "derived_send_to_peer"
        assert tools["send_to_peer"]["targets"] == ["helper"]

        worker = _agent(await (await client.get("/api/scope/bill")).json(), "main", "worker")
        worker_tools = _tools(worker)
        assert worker["replacements"] == []
        assert worker_tools["send_to_agent"]["origin"] == "derived_send_to_agent"
        assert worker_tools["send_to_agent"]["targets"] == ["main"]
        assert "task" not in worker_tools  # leaf: no task tool
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_compiles_capability_declaration_with_registry(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _CAPABILITY_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        response = await client.get("/api/scope/bill")

        assert response.status == 200, await response.text()
        root = _agent(await response.json(), "capability", "root")
        tools = _tools(root)
        assert tools["aci_edit"]["replaces"] == "edit"
        assert root["replacements"] == [
            {
                "default_tool": "edit",
                "replacement_tool": "aci_edit",
                "supplement": "aci",
            }
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_reports_third_party_auto_capability_provenance(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _AUTO_CAPABILITY_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        response = await client.get("/api/scope/bill")

        assert response.status == 200, await response.text()
        root = _agent(await response.json(), "capability", "root")
        assert root["capabilities"] == [
            {
                "capability": "third_party_auto",
                "state": "auto",
                "registration_source": "project",
                "contributions": [
                    {"kind": "tool", "name": "third_party_tool", "gate": "vouched"},
                    {"kind": "hook", "name": "third_party_hook", "gate": "vouched"},
                    {
                        "kind": "section",
                        "name": "third_party_auto.section",
                        "gate": "vouched",
                    },
                ],
            }
        ]
        tools = _tools(root)
        assert tools["third_party_tool"]["origin"] == "capability_derived"
        assert tools["third_party_tool"]["capability"] == "third_party_auto"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_reports_unregistered_capability_error(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _UNKNOWN_CAPABILITY_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        response = await client.get("/api/scope/bill")

        assert response.status == 409
        body = await response.json()
        assert body["error"] == "invalid declaration"
        assert "Component 'unregistered' not found in slot 'capability'" in body["detail"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_recomputes_per_request_no_cache(tmp_path: Path) -> None:
    """SPEC §3.4 hard assertion: rewriting the YAML on disk is reflected in
    the next request without any restart/cache invalidation."""
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        first = await client.get("/api/scope/bill")
        assert first.status == 200, await first.text()
        assert _field(_agent(await first.json(), "main", "main"), "max_steps")["value"] == 50

        path.write_text(
            _WORKSPACE_DECLARATION.replace("max_steps: 50", "max_steps: 75"),
            encoding="utf-8",
        )
        second = await client.get("/api/scope/bill")
        assert second.status == 200, await second.text()
        assert _field(_agent(await second.json(), "main", "main"), "max_steps")["value"] == 75
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_declaration_writes_back_and_bill_reflects_disk(tmp_path: Path) -> None:
    """PoolEditor-pattern write-back: the edited YAML lands on disk, the
    response marks restart-required, and the (unrestarted) bill shows the
    new on-disk declaration (S2)."""
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        edited = _WORKSPACE_DECLARATION.replace("max_steps: 50", "max_steps: 88")
        resp = await client.put("/api/scope/declaration", json={"yaml": edited})
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data == {"saved": True, "restart_required": True}
        assert path.read_text(encoding="utf-8") == edited

        bill = await client.get("/api/scope/bill")
        assert bill.status == 200, await bill.text()
        assert _field(_agent(await bill.json(), "main", "main"), "max_steps")["value"] == 88
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_declaration_validates_capabilities_with_registry(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        response = await client.put(
            "/api/scope/declaration",
            json={"yaml": _CAPABILITY_DECLARATION},
        )

        assert response.status == 200, await response.text()
        assert path.read_text(encoding="utf-8") == _CAPABILITY_DECLARATION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_declaration_capabilities_round_trip(tmp_path: Path) -> None:
    """The capabilities face round-trips through the editor API: PUT a
    declaration carrying a ``capabilities: {todo: {}}`` block, GET it
    back byte-identically, and the recomputed bill reports the capability
    effective (todo tools in the roster)."""
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        declaration = (
            "pool:\n"
            "  name: capability\n"
            "  agents:\n"
            "    root:\n"
            "      capabilities:\n"
            "        todo: {}\n"
        )
        resp = await client.put("/api/scope/declaration", json={"yaml": declaration})
        assert resp.status == 200, await resp.text()

        got = await client.get("/api/scope/declaration")
        assert got.status == 200, await got.text()
        assert (await got.json())["yaml"] == declaration

        bill = await client.get("/api/scope/bill")
        assert bill.status == 200, await bill.text()
        root = _agent(await bill.json(), "capability", "root")
        assert _field(root, "capabilities") == {
            "field": "capabilities",
            "value": ["todo"],
            "layer": "local",
            "profile": None,
        }
        tools = _tools(root)
        assert tools["todo_write"]["origin"] == "capability_derived"
        assert tools["todo_write"]["capability"] == "todo"
        assert tools["todo_read"]["origin"] == "capability_derived"
        assert tools["todo_read"]["capability"] == "todo"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_declaration_rejects_invalid_bodies(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        # Body not matching the request model.
        assert (await client.put("/api/scope/declaration", json={})).status == 400
        # YAML syntax error.
        resp = await client.put("/api/scope/declaration", json={"yaml": "workspace: ["})
        assert resp.status == 400
        # Structural violation: dangling parent reference.
        dangling = _WORKSPACE_DECLARATION.replace("parent-placeholder", "x").replace(
            "worker:\n              description: Child agent.",
            "worker:\n              parent: ghost\n              description: Child agent.",
        )
        resp = await client.put("/api/scope/declaration", json={"yaml": dangling})
        assert resp.status == 400, await resp.text()
        # V6: a child-carrying agent whose wholesale tools list drops task.
        no_task = _WORKSPACE_DECLARATION.replace(
            "max_steps: 50",
            "tools:\n          - read",
        )
        resp = await client.put("/api/scope/declaration", json={"yaml": no_task})
        assert resp.status == 400, await resp.text()
        # Every rejection left the on-disk true source untouched.
        assert path.read_text(encoding="utf-8") == _WORKSPACE_DECLARATION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_409_when_disk_declaration_invalid(tmp_path: Path) -> None:
    """A declaration that loads but fails the tree rules cannot compile —
    the bill endpoint reports the issues instead of serving stale data."""
    _write_declaration(tmp_path, _TWO_ROOTS_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/bill")
        assert resp.status == 409
        data = await resp.json()
        assert data["error"] == "declaration invalid"
        assert any(issue["rule"] == "V3" for issue in data["issues"])
        # The topology endpoint still serves the (broken) structure — it is
        # the declaration shape, not a validity claim.
        topo = await client.get("/api/scope/topology")
        assert topo.status == 200, await topo.text()
    finally:
        await client.close()
