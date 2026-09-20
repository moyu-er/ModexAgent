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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.roots import BotAssemblyRoots
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer
from bot.workspace.dynamic_workspaces import (
    dynamic_declarations_dir,
    dynamic_workspace_declaration_path,
    dynamic_workspace_root,
)
from bot.workspace.handle import PoolWorkspaceResources
from bot.workspace.wiring.resources import _stop_resources
from pydantic import BaseModel, JsonValue

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_router import PoolRoutingStore
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
      description: auto-capability
"""

_SHELL_DECLARATION = """\
pool:
  name: shell
  agents:
    root:
      toolset: none
      capabilities:
        shell:
          mode: terminal
      agents:
        child:
          toolset: none
          capabilities:
            shell:
              mode: terminal
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
        return view.declared.description == "auto-capability"

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
        scope_declaration_path=tmp_path / "config" / "scopes" / "bot.yml",
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


def _obj(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


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


def _hooks(bill_agent: JsonObject) -> dict[str, JsonObject]:
    entries = bill_agent["hooks"]
    assert isinstance(entries, list)
    result: dict[str, JsonObject] = {}
    for entry in entries:
        assert isinstance(entry, dict)
        name = entry.get("hook")
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
        assert agents["main"] == {
            "name": "main",
            "parent": None,
            "root": True,
            "skills_eligible": True,
        }
        assert agents["worker"] == {
            "name": "worker",
            "parent": "main",
            "root": False,
            "skills_eligible": True,
        }
        helper_agents = pools["helper"]["agents"]
        assert helper_agents == [
            {
                "name": "helper",
                "parent": None,
                "root": True,
                "skills_eligible": True,
            }
        ]
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
                "agents": [
                    {
                        "name": "solo",
                        "parent": None,
                        "root": True,
                        "skills_eligible": True,
                    }
                ],
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
        # declared, skills auto-applied to native agents, and subagents
        # auto-applied via children/peers).
        assert _field(main, "capabilities") == {
            "field": "capabilities",
            "value": ["experience", "skills", "subagents", "todo"],
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
                "todo_planning_nudge",
            ],
            "layer": "framework",
            "profile": None,
        }
        hook_rows = _hooks(main)
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
        assert hook_rows["todo_planning_nudge"] == {
            "hook": "todo_planning_nudge",
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
        # No override declared → framework layer; Skills auto-applies to every
        # native agent and subagents auto-applies by non-root position.
        assert _field(worker, "capabilities") == {
            "field": "capabilities",
            "value": ["skills", "subagents"],
            "layer": "framework",
            "profile": None,
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_component_implementation_sources(tmp_path: Path) -> None:
    """The O2 audit surface: per-tool origins (SPEC §3.4). The ``todo``
    capability's contributed tools carry the capability-derived wire
    face; the dedicated aci test below covers the same-name upgrade
    face."""
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/bill")
        assert resp.status == 200, await resp.text()
        main = _agent(await resp.json(), "main", "main")

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
        # Name-slot overwrite face: BOTH entries in the bill — the ``edit``
        # slot is settled at assembly by ToolOrigin rank.
        assert tools["edit"]["origin"] == "preset"
        assert tools["aci_edit"]["origin"] == "capability_derived"
        assert tools["aci_edit"]["capability"] == "aci"
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
                "capability": "skills",
                "state": "auto",
                "registration_source": None,
                "contributions": [
                    {
                        "kind": "section",
                        "name": "skills.injection",
                        "gate": "vouched",
                    }
                ],
            },
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
    effective (todo tools in the roster, plus auto-applied Skills)."""
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
            "value": ["skills", "todo"],
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


# ── Structured model road (pools config panel) ─────────────────────────────


@pytest.mark.asyncio
async def test_get_model_returns_declaration_tree(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/model")
        assert resp.status == 200, await resp.text()
        model = (await resp.json())["model"]
        assert model["workspace"]["name"] == "bot"
        pools = model["workspace"]["pools"]
        assert set(pools) == {"main", "helper"}
        assert pools["main"]["agents"]["main"]["max_steps"] == 50
        assert "worker" in pools["main"]["agents"]["main"]["agents"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_model_writes_canonical_yaml(tmp_path: Path) -> None:
    """The structured save road strips spec/position defaults without
    reintroducing removed root terminal fields."""
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "workspace": {
                "name": "bot",
                "pools": {
                    "main": {
                        "agents": {
                            "main": {
                                "description": "Root of the main pool.",
                                "max_steps": 100,  # spec default — stripped
                                "context_mode": "fresh",  # default — stripped
                                "capabilities": {"todo": {}},
                                "agents": {
                                    "worker": {"description": "Child agent."},
                                },
                            }
                        }
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()
        assert (await resp.json()) == {"saved": True, "restart_required": True}

        text = path.read_text(encoding="utf-8")
        assert "max_steps" not in text
        assert "context_mode" not in text
        assert "use_terminal" not in text
        assert "terminal_visibility" not in text

        got = await client.get("/api/scope/model")
        assert got.status == 200
        reloaded = (await got.json())["model"]
        assert reloaded["workspace"]["pools"]["main"]["agents"]["main"]["capabilities"] == {
            "todo": {}
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_model_rejects_invalid_tree(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        # V3: two roots in one pool.
        two_roots = {
            "pool": {"name": "broken", "agents": {"a": {}, "b": {}}},
        }
        resp = await client.put("/api/scope/model", json={"model": two_roots})
        assert resp.status == 400, await resp.text()
        data = await resp.json()
        assert any(issue["rule"] == "V3" for issue in data["issues"])
        # The on-disk true source is untouched.
        assert path.read_text(encoding="utf-8") == _WORKSPACE_DECLARATION
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_get_options_enumerates_registries(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    mcp_dir = tmp_path / "config" / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "registry.json").write_text(
        '{"mcpServers": {"fetch": {"type": "stdio", "command": "x"}}}',
        encoding="utf-8",
    )
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.get("/api/scope/options")
        assert resp.status == 200, await resp.text()
        data = await resp.json()
        assert data["toolsets"] == ["full", "read_write", "read_only", "none", "web"]
        assert data["context_modes"] == ["fresh", "fork"]
        # DefaultPlugin capabilities + the test's third-party registration.
        assert "aci" in data["capabilities"]
        assert "third_party_auto" in data["capabilities"]
        assert "deliver_retry" in data["default_hooks"]
        assert "deliver_retry" in data["hooks"]
        assert data["mcp_servers"] == ["fetch"]
        # Capability bundles: carried tools/hooks ride the capability and are
        # not free-standing toggles in the panel.
        bundles = data["capability_bundles"]
        assert bundles["aci"]["tools"] == ["aci_edit"]
        assert set(bundles["todo"]["hooks"]) == {
            "todo_continuation",
            "todo_reorientation",
            "todo_planning_nudge",
        }
        assert set(bundles["todo"]["tools"]) == {"todo_write", "todo_read"}
        # Position-dependent bundle contents are unioned across probes.
        assert "subagent_auto_send" in bundles["subagents"]["hooks"]
        assert "task" in bundles["subagents"]["tools"]
        assert bundles["shell"]["tools"] == []
        assert bundles["shell"]["tool_groups"] == [
            {
                "anchor": "bash",
                "origin": "capability_derived",
                "capability": "shell",
                "variants": [
                    {"name": "subprocess", "tools": ["bash"]},
                    {"name": "persistent", "tools": ["bash", "bash_input"]},
                    {"name": "terminal", "tools": ["bash", "process", "terminal"]},
                ],
            }
        ]
        assert bundles["shell"]["config_fields"] == {
            "mode": {
                "value_type": "string",
                "default": "persistent",
                "choices": ["subprocess", "persistent", "terminal"],
            },
            "terminal_visibility": {
                "value_type": "boolean",
                "default": False,
                "choices": [],
            },
        }
        assert data["position_defaults"]["root"] == {
            "toolset": "full",
            "registration": "eager",
        }
        assert data["position_defaults"]["sub"] == {
            "toolset": "read_write",
            "registration": "lazy",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preview_reports_candidate_tool_group_variants(tmp_path: Path) -> None:
    _write_declaration(tmp_path, _SHELL_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = (await (await client.get("/api/scope/model")).json())["model"]
        resp = await client.post("/api/scope/preview", json={"model": model})
        assert resp.status == 200, await resp.text()
        bill = await resp.json()
        root = _agent(bill, "shell", "root")
        assert root["tool_groups"] == [
            {
                "anchor": "bash",
                "origin": "capability_derived",
                "capability": "shell",
                "variants": [
                    {"name": "subprocess", "tools": ["bash"]},
                    {"name": "persistent", "tools": ["bash", "bash_input"]},
                    {"name": "terminal", "tools": ["bash", "process", "terminal"]},
                ],
            }
        ]
        child = _agent(bill, "shell", "child")
        assert child["tool_groups"] == [
            {
                "anchor": "bash",
                "origin": "capability_derived",
                "capability": "shell",
                "variants": [
                    {"name": "subprocess", "tools": ["bash"]},
                    {"name": "persistent", "tools": ["bash", "bash_input"]},
                ],
            }
        ]
    finally:
        await client.close()


@pytest.mark.parametrize(
    ("mode", "variants"),
    [
        ("subprocess", [{"name": "subprocess", "tools": ["bash"]}]),
        (
            "persistent",
            [
                {"name": "subprocess", "tools": ["bash"]},
                {"name": "persistent", "tools": ["bash", "bash_input"]},
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_preview_group_manifest_follows_requested_shell_mode(
    tmp_path: Path,
    mode: str,
    variants: list[JsonObject],
) -> None:
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "pool": {
                "name": "shell",
                "agents": {
                    "root": {
                        "toolset": "none",
                        "capabilities": {"shell": {"mode": mode}},
                    }
                },
            }
        }
        resp = await client.post("/api/scope/preview", json={"model": model})
        assert resp.status == 200, await resp.text()
        root = _agent(await resp.json(), "shell", "root")
        assert root["tool_groups"] == [
            {
                "anchor": "bash",
                "origin": "capability_derived",
                "capability": "shell",
                "variants": variants,
            }
        ]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_put_model_accepts_subagent_terminal_request_without_rewriting(
    tmp_path: Path,
) -> None:
    path = _write_declaration(tmp_path, _SHELL_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = (await (await client.get("/api/scope/model")).json())["model"]
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()
        saved = path.read_text(encoding="utf-8")
        assert saved.count("mode: terminal") == 2
        reloaded = (await (await client.get("/api/scope/model")).json())["model"]
        child = reloaded["pool"]["agents"]["root"]["agents"]["child"]
        assert child["capabilities"]["shell"]["mode"] == "terminal"
    finally:
        await client.close()


# ── Draft preview road (live effective state) ──────────────────────────────


@pytest.mark.asyncio
async def test_preview_returns_draft_bill_without_writing(tmp_path: Path) -> None:
    """The preview compiles the DRAFT (max_steps 88, todo capability dropped)
    and the on-disk true source stays byte-identical."""
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    before = path.read_text(encoding="utf-8")
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        draft = {
            "workspace": {
                "name": "bot",
                "pools": {
                    "main": {
                        "agents": {
                            "main": {
                                "description": "Root of the main pool.",
                                "max_steps": 88,
                                # todo capability dropped from the draft.
                                "capabilities": {"experience": {}},
                                "agents": {
                                    "worker": {"description": "Child agent."},
                                },
                            }
                        }
                    },
                    "helper": {"agents": {"helper": {"description": "x"}}},
                },
            }
        }
        resp = await client.post("/api/scope/preview", json={"model": draft})
        assert resp.status == 200, await resp.text()
        bill = await resp.json()
        main = _agent(bill, "main", "main")
        assert _field(main, "max_steps")["value"] == 88
        # The dropped capability's bundle hooks are gone from the effective
        # roster; declared/default hooks stay.
        hooks = set(_hooks(main))
        assert "todo_continuation" not in hooks
        assert "deliver_retry" in hooks
        # Nothing written.
        assert path.read_text(encoding="utf-8") == before
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preview_rejects_invalid_draft(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        resp = await client.post(
            "/api/scope/preview",
            json={"model": {"pool": {"name": "broken", "agents": {"a": {}, "b": {}}}}},
        )
        assert resp.status == 400, await resp.text()
        data = await resp.json()
        assert any(issue["rule"] == "V3" for issue in data["issues"])
        assert path.read_text(encoding="utf-8") == _WORKSPACE_DECLARATION
    finally:
        await client.close()


# ── Effective memory/approval projection (PA-10) ────────────────────────────


_MEMORY_DECLARATION = """\
workspace:
  name: bot
  pools:
    main:
      agents:
        main:
          description: Root with archive on, core on.
          memory:
            archive_enabled: true
            core_enabled: true
            session:
              max_context_tokens: 12345
          approval:
            enabled: true
          agents:
            worker:
              description: Session-only child.
              memory:
                session:
                  max_context_tokens: 999
"""

_EXTERNAL_DECLARATION = """\
workspace:
  name: bot
  pools:
    opencode:
      agents:
        opencode:
          description: External root.
          execution_strategy: external
          provider_kind: opencode
"""


async def _get_bill_json(client: TestClient) -> JsonObject:
    resp = await client.get("/api/scope/bill")
    assert resp.status == 200, await resp.text()
    data: JsonObject = await resp.json()
    return data


@pytest.mark.asyncio
async def test_bill_reports_effective_memory_and_approval(tmp_path: Path) -> None:
    """The effective face: declared root toggles on; explicit approval on;
    absent approval is off (the config default), never missing."""
    _write_declaration(tmp_path, _MEMORY_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        bill = await _get_bill_json(client)

        main = _agent(bill, "main", "main")
        assert main["memory"] == {
            "memory_preset": "archive_core",
            "archive_enabled": True,
            "core_enabled": True,
        }
        assert main["approval"] == {"enabled": True, "eligible": True}
        assert main["external"] is False

        # Non-root stays session-only regardless of any declared toggles;
        # approval is position-ineligible (V9).
        worker = _agent(bill, "main", "worker")
        assert worker["memory"] == {
            "memory_preset": "session_only",
            "archive_enabled": False,
            "core_enabled": False,
        }
        assert worker["approval"] == {"enabled": False, "eligible": False}
        assert worker["external"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_defaults_are_off_not_missing(tmp_path: Path) -> None:
    """No memory/approval blocks anywhere: a root's effective toggles are
    the position defaults (off/off, approval off) — the UI shows real
    effective values, never interprets absence as "unknown"."""
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        bill = await _get_bill_json(client)
        main = _agent(bill, "main", "main")
        assert main["memory"] == {
            "memory_preset": "archive_core",
            "archive_enabled": False,
            "core_enabled": False,
        }
        assert main["approval"] == {"enabled": False, "eligible": True}
        worker = _agent(bill, "main", "worker")
        assert worker["approval"] == {"enabled": False, "eligible": False}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bill_reports_external_agent_face(tmp_path: Path) -> None:
    """External agents report their actual applicability: external=True,
    memory shows the declared (structurally unused) position face, approval
    is not applicable."""
    _write_declaration(tmp_path, _EXTERNAL_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        bill = await _get_bill_json(client)
        ext = _agent(bill, "opencode", "opencode")
        assert ext["external"] is True
        assert ext["root"] is True
        # MED4: external has no native approval channel — not applicable.
        assert ext["approval"] == {"enabled": False, "eligible": False}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preview_effective_matches_disk_projection(tmp_path: Path) -> None:
    """Same declaration through the preview road yields the same effective
    memory/approval — one projection function, two entrances."""
    _write_declaration(tmp_path, _MEMORY_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = (await (await client.get("/api/scope/model")).json())["model"]
        resp = await client.post("/api/scope/preview", json={"model": model})
        assert resp.status == 200, await resp.text()
        preview: JsonObject = await resp.json()

        disk = await _get_bill_json(client)
        for face in ("main", "worker"):
            draft_agent = _agent(preview, "main", face)
            disk_agent = _agent(disk, "main", face)
            assert draft_agent["memory"] == disk_agent["memory"]
            assert draft_agent["approval"] == disk_agent["approval"]
            assert draft_agent["external"] == disk_agent["external"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_preview_tracks_draft_memory_and_approval_changes(tmp_path: Path) -> None:
    """A draft that flips memory off and approval off previews the flipped
    effective values without touching the disk declaration."""
    path = _write_declaration(tmp_path, _MEMORY_DECLARATION)
    before = path.read_text(encoding="utf-8")
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        draft = {
            "workspace": {
                "name": "bot",
                "pools": {
                    "main": {
                        "agents": {
                            "main": {
                                "description": "Explicit off survives.",
                                # memory explicitly OFF (archive false) while
                                # keeping the nested session override — the
                                # canonical writer must preserve both.
                                "memory": {
                                    "archive_enabled": False,
                                    "session": {"max_context_tokens": 12345},
                                },
                                "approval": {"enabled": False},
                                "agents": {
                                    "worker": {"description": "Child agent."},
                                },
                            }
                        }
                    }
                },
            }
        }
        resp = await client.post("/api/scope/preview", json={"model": draft})
        assert resp.status == 200, await resp.text()
        main = _agent(await resp.json(), "main", "main")
        assert main["memory"] == {
            "memory_preset": "archive_core",
            "archive_enabled": False,
            "core_enabled": False,
        }
        assert main["approval"] == {"enabled": False, "eligible": True}
        assert path.read_text(encoding="utf-8") == before
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_memory_explicit_false_survives_canonical_reload(tmp_path: Path) -> None:
    """Save an explicit archive_enabled: false (semantic OFF) through the
    structured road: the canonical deviations-only serializer may drop a
    false that equals the default, and the reloaded bill must still read
    archive off — the effective value, not the YAML text, is the contract.
    The nested session override survives either way."""
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "pool": {
                "name": "solo",
                "agents": {
                    "solo": {
                        "description": "Off must stay off.",
                        "memory": {
                            "archive_enabled": False,
                            "session": {"max_context_tokens": 4321},
                        },
                        "approval": {"enabled": False, "tools": {}},
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()

        reloaded = (await (await client.get("/api/scope/model")).json())["model"]
        saved_memory = reloaded["pool"]["agents"]["solo"]["memory"]
        # Deviations-only: archive_enabled=false equals the field default and
        # MAY be dropped — but then the effective value must still be off.
        archive = saved_memory.get("archive_enabled", False)
        assert archive is False
        # The non-default nested override is preserved verbatim.
        assert saved_memory["session"] == {"max_context_tokens": 4321}

        bill = await _get_bill_json(client)
        solo = _agent(bill, "solo", "solo")
        assert _obj(solo["memory"])["archive_enabled"] is False
        assert _obj(solo["memory"])["core_enabled"] is False
        assert solo["approval"] == {"enabled": False, "eligible": True}

        # Disk text: whatever the serializer kept, reloading it never turns
        # an explicit off back into the inherited default-on.
        text = (tmp_path / "config" / "scopes" / "bot.yml").read_text(encoding="utf-8")
        assert "archive_enabled: true" not in text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_memory_core_requires_archive_rejected(tmp_path: Path) -> None:
    """The schema's AND rule is enforced by the original owner (the loader
    model validator): core without archive fails the gate chain."""
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "pool": {
                "name": "solo",
                "agents": {
                    "solo": {
                        "description": "Invalid.",
                        "memory": {"archive_enabled": False, "core_enabled": True},
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 400, await resp.text()
        body = await resp.json()
        assert "core_enabled=True requires archive_enabled=True" in str(body["detail"])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_memory_archive_off_cascades_core_and_keeps_session(tmp_path: Path) -> None:
    """Turning archive off while core was on (the UI's dependency cascade)
    is a valid save; the nested session override survives it."""
    _write_declaration(tmp_path, _MEMORY_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "workspace": {
                "name": "bot",
                "pools": {
                    "main": {
                        "agents": {
                            "main": {
                                "description": "Cascade off.",
                                "memory": {
                                    "archive_enabled": False,
                                    "core_enabled": False,
                                    "session": {"max_context_tokens": 12345},
                                },
                                "agents": {"worker": {"description": "Child."}},
                            }
                        }
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()
        bill = await _get_bill_json(client)
        main = _agent(bill, "main", "main")
        assert _obj(main["memory"])["archive_enabled"] is False
        assert _obj(main["memory"])["core_enabled"] is False
        reloaded = (await (await client.get("/api/scope/model")).json())["model"]
        memory = reloaded["workspace"]["pools"]["main"]["agents"]["main"]["memory"]
        assert memory.get("session") == {"max_context_tokens": 12345}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_approval_off_preserves_tools_map(tmp_path: Path) -> None:
    """approval.enabled=false keeps the unrelated per-tool rules block —
    turning approval off is not deleting the advanced config."""
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "pool": {
                "name": "solo",
                "agents": {
                    "solo": {
                        "description": "Keep tools.",
                        "approval": {
                            "enabled": False,
                            "tools": {"bash": {"allowed_paths": ["./*"]}},
                        },
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()
        reloaded = (await (await client.get("/api/scope/model")).json())["model"]
        approval = reloaded["pool"]["agents"]["solo"]["approval"]
        # Deviations-only may drop `enabled: false` (the field default) —
        # semantic OFF survives because absence inherits the off default.
        assert approval.get("enabled", False) is False
        # The unrelated per-tool rules block is preserved verbatim.
        assert approval["tools"] == {"bash": {"allowed_paths": ["./*"]}}

        bill = await _get_bill_json(client)
        solo = _agent(bill, "solo", "solo")
        assert solo["approval"] == {"enabled": False, "eligible": True}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reset_clears_only_local_override(tmp_path: Path) -> None:
    """Removing the memory/approval override blocks (reset-to-default) —
    the canonical reload keeps every unrelated advanced field on the same
    agent (a custom plugin config key rides along untouched)."""
    _write_declaration(tmp_path, _POOL_ROOT_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "pool": {
                "name": "solo",
                "agents": {
                    "solo": {
                        "description": "Keep siblings.",
                        "capabilities": {"experience": {}},
                        "memory": {"archive_enabled": True, "core_enabled": True},
                        "approval": {"enabled": True},
                        "llm_provider_config": {"custom_key": "custom_value"},
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()

        reloaded = (await (await client.get("/api/scope/model")).json())["model"]
        body = reloaded["pool"]["agents"]["solo"]
        assert body["memory"] == {"archive_enabled": True, "core_enabled": True}
        assert body["approval"] == {"enabled": True}
        assert body["capabilities"] == {"experience": {}}
        assert body["llm_provider_config"] == {"custom_key": "custom_value"}

        # Reset: drop only the memory override; everything else persists.
        del body["memory"]
        resp = await client.put("/api/scope/model", json={"model": reloaded})
        assert resp.status == 200, await resp.text()
        final = (await (await client.get("/api/scope/model")).json())["model"]
        solo = final["pool"]["agents"]["solo"]
        assert "memory" not in solo
        assert solo["approval"] == {"enabled": True}
        assert solo["capabilities"] == {"experience": {}}
        assert solo["llm_provider_config"] == {"custom_key": "custom_value"}

        bill = await _get_bill_json(client)
        entry = _agent(bill, "solo", "solo")
        assert _obj(entry["memory"])["archive_enabled"] is False  # position default
        assert entry["approval"] == {"enabled": True, "eligible": True}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_capability_and_hook_semantics_survive_effective_save(tmp_path: Path) -> None:
    """The unified mutation semantics on one save: capability off writes
    explicit false (kept by the serializer), capability auto drops the key,
    and the hook roster's +/- entries survive verbatim."""
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        model = {
            "workspace": {
                "name": "bot",
                "pools": {
                    "main": {
                        "agents": {
                            "main": {
                                "description": "Root.",
                                "capabilities": {
                                    "todo": False,  # explicit off stays false
                                    "experience": {},  # on with default config
                                    # skills: auto (absent) — stays absent
                                },
                                "hooks": ["+reference_collector", "-length_guard"],
                                "agents": {"worker": {"description": "Child."}},
                            }
                        }
                    }
                },
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()
        reloaded = (await (await client.get("/api/scope/model")).json())["model"]
        body = reloaded["workspace"]["pools"]["main"]["agents"]["main"]
        assert body["capabilities"] == {
            "todo": False,
            "experience": {},
        }
        assert body["hooks"] == ["+reference_collector", "-length_guard"]

        bill = await _get_bill_json(client)
        entry = _agent(bill, "main", "main")
        capabilities = entry["capabilities"]
        assert isinstance(capabilities, list)
        states = {
            _obj(c)["capability"]: _obj(c)["state"]
            for c in capabilities
            if isinstance(c, dict)
        }
        # `todo: false` on a declaration-driven (non-auto) capability simply
        # keeps it absent — no veto provenance, no contributed tools. The
        # declared `experience: {}` force-enables; `skills` stays auto.
        assert "todo" not in states
        assert "todo_write" not in _tools(entry)
        assert states["experience"] == "declared"
        assert states["skills"] == "auto"
        effective_hooks = _hooks(entry)
        assert "length_guard" not in effective_hooks
        assert "reference_collector" in effective_hooks
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_scope_endpoints_target_request_workspace(tmp_path: Path) -> None:
    """Workspace targeting: with ?ws= set, every scope road reads and
    writes the RESOLVED workspace's boot-selected declaration, never
    another workspace's file."""
    other_root = tmp_path / "other"
    _write_declaration(other_root, _POOL_ROOT_DECLARATION)
    # Home carries a DIFFERENT declaration (workspace form).
    _write_declaration(tmp_path, _WORKSPACE_DECLARATION)

    server = _make_server(tmp_path)
    resolved: dict[str, object] = {}

    def resolver(ws: str) -> object:
        target = other_root if ws == "other" else tmp_path
        resolved["last_ws"] = ws
        return SimpleNamespace(
            target=target,
            ctx=WorkspaceContext.from_target(
                target, data_dir_name=".modex", home=tmp_path
            ),
            component_registry=_component_registry(),
            # The boot-selected declaration each target actually booted from
            # (HIGH3: the routes must consume this, not a per-cwd guess).
            scope_declaration_path=_boot_declaration_path(target),
        )

    server.set_graph_workspace_resolver(resolver)  # type: ignore[arg-type]
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # Reads resolve the requested workspace's file.
        bill = await client.get("/api/scope/bill", params={"ws": "other"})
        assert bill.status == 200, await bill.text()
        data: JsonObject = await bill.json()
        assert _agent(data, "solo", "solo")["root"] is True
        assert resolved["last_ws"] == "other"

        # Writes land in the same workspace — home's declaration untouched.
        model = {
            "pool": {
                "name": "solo",
                "agents": {"solo": {"description": "edited", "max_steps": 42}},
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model}, params={"ws": "other"})
        assert resp.status == 200, await resp.text()
        other_text = (other_root / "config" / "scopes" / "bot.yml").read_text(
            encoding="utf-8"
        )
        assert "max_steps: 42" in other_text
        home_text = (tmp_path / "config" / "scopes" / "bot.yml").read_text(
            encoding="utf-8"
        )
        assert "max_steps: 42" not in home_text
        assert "helper" in home_text  # still the workspace-form declaration
    finally:
        await client.close()


def _boot_declaration_path(target: Path) -> Path:
    """The boot-selected declaration for a stub bundle: mirrors the
    resources-wiring rule (primary roots declaration; a dynamic workspace
    target boots its per-name file when present)."""
    dynamic = dynamic_workspace_declaration_path(target, target)
    if dynamic is not None:
        return dynamic
    return target / "config" / "scopes" / "bot.yml"


# ── Boot-selected declaration path (HIGH3) ──────────────────────────────────
#
# The routes must edit THE DECLARATION THE WORKSPACE BOOTED FROM: the service
# roots' primary declaration (config root — possibly an external config dir,
# not the workspace target) or a dynamic workspace's per-name file under the
# resource root. The fixtures below build REAL workspace bundles through the
# production ``_build_resources`` road and serve the scope API over them.


_MINIMAL_POOL_DECL = """\
pool:
  name: main
  agents:
    main:
      description: declared main agent
"""


def _write_bot_project(project_dir: Path, declaration: str) -> None:
    """A minimal bootable bot project (agents prompt + declaration)."""
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)
    (project_dir / "agents" / "main.md").write_text(
        "You are a helpful assistant. Reply briefly.\n", encoding="utf-8"
    )
    scopes = project_dir / "config" / "scopes"
    scopes.mkdir(parents=True, exist_ok=True)
    (scopes / "bot.yml").write_text(declaration, encoding="utf-8")


def _boot_service_stub(project_dir: Path, tmp_home: Path) -> MagicMock:
    """A service stub carrying REAL ``BotAssemblyRoots`` — the same shape
    ``test_workspace_resource_declaration._service`` uses."""
    service = MagicMock()
    service.roots = BotAssemblyRoots.resident(
        config_dir=project_dir / "config", resource_root=project_dir
    )
    service._enable_dynamic_workspaces = True
    service._app_config = AppConfig.model_validate(
        {"persistence": {"backend": "file"}, "paths": {"data_dir_name": ".modex"}}
    )
    service._home_persistence = None
    service._mcp_registry = None
    service._bot_model_config = None
    service._default_provider = None
    service._default_pool_name = "main"
    service._pool_session_store = MagicMock(spec=PoolRoutingStore)
    service._model_choice_registry = None
    service._transcript_store = None
    service._output_adapter_factory = None
    service._on_subagent_created = None
    service.control_channel = MagicMock()
    service.command_processor = MagicMock()
    service.workspace_stack = None
    service._strategy_registry = None
    # The full DefaultPlugin registry already carries the file_prompt
    # SYSTEM_PROMPT_PROVIDER factory — no extra registration needed.
    service._component_registry = _component_registry()
    return service


async def _boot_resources(service: MagicMock, target: Path) -> PoolWorkspaceResources:
    """Materialize one workspace through the REAL ``_build_resources``."""
    from bot.workspace.wiring.resources import _build_resources

    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=target)

    async def _create_pool(**kwargs: Any) -> MagicMock:
        instance = MagicMock()
        instance.root_agent_name = "main"
        instance.pool._agents = {}
        instance.pool.shutdown_all = AsyncMock(return_value=True)
        instance.broker_bridge.start = AsyncMock()
        instance.mcp_manager = None
        return instance

    with (
        patch("bot.service.pool.create_pool", side_effect=_create_pool),
        patch("bot.workspace.wiring.resources.BackgroundTaskRunner") as background_type,
    ):
        background_type.return_value.start = AsyncMock()
        background_type.return_value.stop = AsyncMock()
        resources = await _build_resources(service, ctx)
    return resources


@pytest.mark.asyncio
async def test_scope_routes_use_boot_selected_declaration_external_config_root(
    tmp_path: Path,
) -> None:
    """Ordinary deployment with an EXTERNAL config dir: the declaration the
    workspace booted from lives in the CONFIG root (``--config``), NOT under
    the workspace target. The scope API must read/edit that booted file —
    a target-relative path would invent a per-cwd config that nothing
    boots from."""
    project_dir = tmp_path / "bot-assets"
    config_dir = tmp_path / "actual-config"
    workspace_root = tmp_path / "ide-workspace"
    (project_dir / "agents").mkdir(parents=True)
    (project_dir / "agents" / "main.md").write_text("main\n", encoding="utf-8")
    (config_dir / "scopes").mkdir(parents=True)
    (config_dir / "scopes" / "bot.yml").write_text(_MINIMAL_POOL_DECL, encoding="utf-8")
    workspace_root.mkdir()

    service = _boot_service_stub(project_dir, tmp_path)
    service.roots = BotAssemblyRoots(
        config_dir=config_dir,
        resource_root=project_dir,
        workspace_home=workspace_root,
    )
    service._enable_dynamic_workspaces = False
    resources = await _boot_resources(service, workspace_root)

    server = _make_server(tmp_path)
    server.set_graph_workspace_resolver(lambda ws: resources)  # type: ignore[arg-type]
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # The bundle carries the boot-selected declaration: the CONFIG-root
        # file, not <workspace_root>/config/scopes/bot.yml (which does not
        # even exist).
        assert resources.scope_declaration_path == config_dir / "scopes" / "bot.yml"
        assert not (workspace_root / "config" / "scopes" / "bot.yml").exists()

        bill = await client.get("/api/scope/bill")
        assert bill.status == 200, await bill.text()
        data: JsonObject = await bill.json()
        assert _agent(data, "main", "main")["root"] is True

        # The structured save writes the BOOTED file in the config root.
        model = {
            "pool": {
                "name": "main",
                "agents": {"main": {"description": "edited", "max_steps": 42}},
            }
        }
        resp = await client.put("/api/scope/model", json={"model": model})
        assert resp.status == 200, await resp.text()
        text = (config_dir / "scopes" / "bot.yml").read_text(encoding="utf-8")
        assert "max_steps: 42" in text
    finally:
        await client.close()
        await _stop_resources(resources)


@pytest.mark.asyncio
async def test_scope_routes_use_dynamic_workspace_declaration(tmp_path: Path) -> None:
    """A runtime-created (dynamic) workspace boots ITS OWN declaration file
    under the resource root's ``config/scopes/workspaces/<name>.yml``. The
    scope API over that workspace must edit the per-name file — the primary
    declaration (served for home) stays untouched."""
    project_dir = tmp_path / "proj"
    _write_bot_project(project_dir, _MINIMAL_POOL_DECL)
    # The dynamic workspace's declaration: a DIFFERENT pool name, so a mixup
    # is observable in the bill.
    dynamic_root = dynamic_workspace_root(project_dir, "ws-alpha")
    dynamic_root.mkdir(parents=True)
    dynamic_decl = dynamic_declarations_dir(project_dir) / "ws-alpha.yml"
    dynamic_decl.parent.mkdir(parents=True, exist_ok=True)
    dynamic_decl.write_text(
        "pool:\n"
        "  name: alpha\n"
        "  agents:\n"
        "    alpha:\n"
        "      description: dynamic root\n",
        encoding="utf-8",
    )

    service = _boot_service_stub(project_dir, tmp_path)
    resources = await _boot_resources(service, dynamic_root)

    server = _make_server(tmp_path)
    server.set_graph_workspace_resolver(lambda ws: resources)  # type: ignore[arg-type]
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        assert resources.scope_declaration_path == dynamic_decl

        bill = await client.get("/api/scope/bill")
        assert bill.status == 200, await bill.text()
        data: JsonObject = await bill.json()
        assert _agent(data, "alpha", "alpha")["root"] is True

        # Preview + save target the dynamic file; the primary declaration
        # stays byte-identical.
        primary_before = (project_dir / "config" / "scopes" / "bot.yml").read_text(
            encoding="utf-8"
        )
        draft = {
            "pool": {
                "name": "alpha",
                "agents": {"alpha": {"description": "dyn", "max_steps": 33}},
            }
        }
        preview = await client.post("/api/scope/preview", json={"model": draft})
        assert preview.status == 200, await preview.text()
        resp = await client.put("/api/scope/model", json={"model": draft})
        assert resp.status == 200, await resp.text()
        assert "max_steps: 33" in dynamic_decl.read_text(encoding="utf-8")
        assert (
            project_dir / "config" / "scopes" / "bot.yml"
        ).read_text(encoding="utf-8") == primary_before
    finally:
        await client.close()
        await _stop_resources(resources)


@pytest.mark.asyncio
async def test_external_declared_approval_reports_not_applicable(tmp_path: Path) -> None:
    """MED4: the schema ACCEPTS an external root declaring approval — the
    bill must still report approval NOT applicable (enabled/eligible
    false): external agents have no native approval channel, and the
    friendly form must not claim support it cannot deliver."""
    declaration = (
        "pool:\n"
        "  name: opencode\n"
        "  agents:\n"
        "    opencode:\n"
        "      description: External root with a declared approval block.\n"
        "      execution_strategy: external\n"
        "      provider_kind: opencode\n"
        "      approval:\n"
        "        enabled: true\n"
    )
    _write_declaration(tmp_path, declaration)
    client = _make_client(tmp_path)
    await client.start_server()
    try:
        bill = await _get_bill_json(client)
        ext = _agent(bill, "opencode", "opencode")
        assert ext["external"] is True
        assert ext["approval"] == {"enabled": False, "eligible": False}
    finally:
        await client.close()
