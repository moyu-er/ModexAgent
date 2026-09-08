"""The ``subagents`` capability's supply + assemble wave (SPEC §8.4).

Pins the FW-化 of the retired BIZ communication assembly:
- ``supply()`` builds the pool's communication service from the
  aggregation view's skeleton handles (constructor parity with the
  retired BIZ ``create_pool`` site — the same objects, the same shape);
- ``assemble()`` builds the per-agent target store from the DECLARED
  tree and binds the root's store onto the service;
- the section providers: wiring shape + the peer brief's live-store
  dynamics (the static briefs' text is deliberately NOT pinned
  byte-for-byte — static prompt content makes a meaningless test);
- the dark supply: a zero-topology pool compiles no capability, so a
  hand-referenced communication entry loud-raises at the factory;
- the typed carriers the supply replaced are gone (death greps).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from modex_agent.core.agent import AgentCommKind, ExecutionStrategyKind
from modex_agent.core.agent import AgentContext as RuntimeAgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.service import _TracePropagatingPeerNormal
from modex_agent.multi_agent.communication.strategies.base import SendStrategyKind
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToAgentTool,
)
from modex_agent.plugins.assembly.context import PoolRuntimeDeps, SupplyInfra
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    AgentDeclaredFields,
    CapabilityBinding,
    PromptSectionSpec,
)
from modex_agent.plugins.defaults.capabilities.subagents import (
    SubagentsCapability,
    SubagentsSupply,
    build_pool_communication_service,
)
from modex_agent.plugins.defaults.communication import (
    SendToAgentToolFactory,
    TaskToolFactory,
)
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_CAPABILITY = SubagentsCapability()


def _view(**overrides: Any) -> Any:
    """A PoolSupplyView shaped like the aggregation's (skeleton handles)."""
    from modex_agent.plugins.capability import PoolSupplyView

    fields: dict[str, Any] = {
        "pool_name": "default",
        "entries": (),
        "root_agent_name": "main",
        "pool": MagicMock(name="pool"),
        "session_tree": MagicMock(name="tree"),
        "template_registry": MagicMock(name="template_registry"),
        "session_registry": MagicMock(name="session_registry"),
        "scope_path": MagicMock(name="scope_path"),
        "workspace_manager": MagicMock(name="workspace_manager"),
        "project_dir": Path("/w"),
        "trace_enabled": False,
    }
    fields.update(overrides)
    return PoolSupplyView(**fields)


class TestSupplyConstructionParity:
    """The service resolves the SAME skeleton deps the retired BIZ passed."""

    def test_supply_builds_service_from_view_handles(self) -> None:
        pool = MagicMock(name="pool")
        tree = MagicMock(name="tree")
        template_registry = MagicMock(name="template_registry")
        session_registry = MagicMock(name="session_registry")
        scope_path = MagicMock(name="scope_path")
        workspace_manager = MagicMock(name="workspace_manager")

        supply = _CAPABILITY.supply(
            _view(
                pool=pool,
                session_tree=tree,
                template_registry=template_registry,
                session_registry=session_registry,
                scope_path=scope_path,
                workspace_manager=workspace_manager,
                project_dir=Path("/w"),
                trace_enabled=False,
            )
        )

        assert isinstance(supply, SubagentsSupply)
        service = supply.service
        # The retired BIZ construction's exact argument set (service.py
        # stores them on the instance — reading the privates is the
        # parity proof).
        assert service._source == AgentAddress(name="main")
        assert service._registry is pool
        assert service._tree is tree
        assert service._template_registry is template_registry
        assert service._pool is pool
        assert service._pool_name == "default"
        assert service._project_dir == Path("/w")
        assert service._session_registry is session_registry
        deps = service._strategies[SendStrategyKind.SUBAGENT_DISPATCH]._deps
        assert deps.scope_path is scope_path
        assert deps.workspace_manager is workspace_manager
        assert deps.trace_enabled is False
        assert service._target_store is None  # assemble-phase business

    def test_traceparent_strategy_rides_the_construction(self) -> None:
        # The FW construction reproduces the retired BIZ path exactly:
        # the PEER_NORMAL strategy is the traceparent-propagating one.
        supply = _CAPABILITY.supply(_view())
        assert isinstance(
            supply.service._strategies[SendStrategyKind.PEER_NORMAL],
            _TracePropagatingPeerNormal,
        )

    def test_trace_enabled_defaults_true(self) -> None:
        supply = _CAPABILITY.supply(_view(trace_enabled=True))
        peer = supply.service._strategies[SendStrategyKind.PEER_NORMAL]
        assert peer._deps.trace_enabled is True

    def test_no_lifecycle(self) -> None:
        # D4: the service is a router, not a background task — the
        # CapabilitySupply no-op defaults are the whole lifecycle.

        assert "start" not in SubagentsSupply.__dict__
        assert "stop" not in SubagentsSupply.__dict__

    def test_loud_raise_without_root_agent_name(self) -> None:
        with pytest.raises(ValueError, match="root agent name"):
            _CAPABILITY.supply(_view(root_agent_name=None))

    def test_loud_raise_without_pool_handles(self) -> None:
        with pytest.raises(ValueError, match="AgentPool / session tree"):
            _CAPABILITY.supply(_view(pool=None, session_tree=None))

    def test_builder_is_the_single_construction_authority(self) -> None:
        # The BIZ capability-less fallback and supply() share ONE builder:
        # both produce the same router shape from the same handles.
        pool = MagicMock(name="pool")
        tree = MagicMock(name="tree")
        service = build_pool_communication_service(
            root_agent_name="main",
            pool=pool,
            tree=tree,
            pool_name="default",
        )
        assert service._source == AgentAddress(name="main")
        assert service._registry is pool
        assert service._tree is tree
        assert service._pool_name == "default"
        assert service._target_store is None


class _Topology:
    """A three-level declared tree + the chain faces for assemble."""

    ROOT = "main"
    MID = "mid"
    LEAF = "leaf"

    @classmethod
    def pool_spec(cls) -> PoolSpec:
        return PoolSpec(
            name="default",
            agents=[
                AgentSpec(name=cls.ROOT, description="the main agent"),
                AgentSpec(name=cls.MID, description="middle", parent=cls.ROOT),
                AgentSpec(name=cls.LEAF, description="leaf", parent=cls.MID),
            ],
        )

    @classmethod
    def pool_assembly(cls) -> PoolAssemblyContext:
        return PoolAssemblyContext(
            pool_name="default",
            pool_spec=cls.pool_spec(),
            project_dir=Path("."),
            data_dir=Path("."),
            broker=MagicMock(),
            inbox_server=MagicMock(),
            agent_bus=MagicMock(),
            output_adapter=MagicMock(),
            safety=MagicMock(),
            retention=MagicMock(),
            registry=MagicMock(),
        )

    @classmethod
    def spec(cls, agent_name: str, agent_type: str) -> AssemblySpec:
        return AssemblySpec(
            agent_name=agent_name,
            pool_name="default",
            execution_strategy=ExecutionStrategyKind.REACT.value,
            agent_type=agent_type,  # type: ignore[arg-type]
            tools=[],
            hooks=[],
            llm_provider="default",
            system_prompt_provider="file_prompt",
            system_prompt_config={},
            memory_overrides=MemoryOverrides(),
            workspace_ctx=WorkspaceContext(
                target=Path("."), paths=WorkspacePaths(root=Path(".")), is_home=False
            ),
        )

    @classmethod
    def chain(cls, agent_name: str, agent_type: str) -> Any:
        from modex_agent.plugins.assembly.context import AgentContext

        return AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(),
                paths=WorkspacePaths(root=MagicMock()),
                is_home=False,
            ),
            pool_runtime=PoolRuntimeDeps(
                pool_assembly_ctx=cls.pool_assembly(),
                capability_supply={"subagents": SubagentsSupply(service=MagicMock())},
            ),
            agent_name=agent_name,
            spec=cls.spec(agent_name, agent_type),
        )


class TestPerAgentStores:
    async def test_main_store_from_children(self) -> None:
        wiring = await _CAPABILITY.assemble(
            CapabilityBinding(), _Topology.chain(_Topology.ROOT, "native_main")
        )
        store = wiring.artifacts["target_store"]
        assert [t.name for t in store.list_subagents()] == [_Topology.MID]

    async def test_sub_store_from_own_children(self) -> None:
        wiring = await _CAPABILITY.assemble(
            CapabilityBinding(), _Topology.chain(_Topology.MID, "native_sub")
        )
        store = wiring.artifacts["target_store"]
        assert [t.name for t in store.list_subagents()] == [_Topology.LEAF]

    async def test_send_to_agent_factory_builds_subagent_mode_store(self) -> None:
        # The leaf's consultation tool: a fresh subagent-mode store, the
        # parent resolved dynamically at execution time. The chain carries
        # the leaf's compiled spec (native_sub) — the factory's position
        # gate reads it.
        import dataclasses

        wiring = await _CAPABILITY.assemble(
            CapabilityBinding(), _Topology.chain(_Topology.LEAF, "native_sub")
        )
        chain = dataclasses.replace(
            _Topology.chain(_Topology.LEAF, "native_sub"),
            capability_wirings={"subagents": wiring},
        )
        tool = await SendToAgentToolFactory().create(_empty_config(), chain)
        assert tool.name == "send_to_agent"
        assert isinstance(tool, SendToAgentTool)
        assert tool.list_targets() == []  # parent resolves at call time

    async def test_send_to_agent_factory_rejects_root_agent(self) -> None:
        """Position gate: a root agent's hand reference cannot resolve the
        subagent→parent consultation tool — configuration alone must not
        enable a tool the tree position does not carry."""
        chain = _Topology.chain(_Topology.ROOT, "native_main")
        with pytest.raises(ValueError, match=r"not a subagent"):
            await SendToAgentToolFactory().create(_empty_config(), chain)

    async def test_send_to_agent_factory_rejects_specless_chain(self) -> None:
        """A hand-built chain without a spec carries no position signal —
        rejected loudly (the gate may never guess)."""
        from modex_agent.plugins.assembly.context import AgentContext

        chain = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
            ),
            pool_runtime=PoolRuntimeDeps(
                capability_supply={"subagents": SubagentsSupply(service=MagicMock())}
            ),
            agent_name=_Topology.LEAF,
        )
        with pytest.raises(ValueError, match=r"not a subagent"):
            await SendToAgentToolFactory().create(_empty_config(), chain)


class TestSectionProviders:
    """The section providers: wiring shape + the peer brief's live-store
    dynamics."""

    @staticmethod
    def _binding(*section_ids: str) -> CapabilityBinding:
        return CapabilityBinding(
            active_sections=tuple(
                PromptSectionSpec(section_id=sid, order=40 + i) for i, sid in enumerate(section_ids)
            )
        )

    async def test_peer_brief_empty_until_peers_join(self) -> None:
        wiring = await _CAPABILITY.assemble(
            self._binding("subagents.peer"),
            _Topology.chain(_Topology.ROOT, "native_main"),
        )
        provider = wiring.prompt_providers[0]
        assert await provider.get_or_refresh() == ""

    async def test_peer_brief_tracks_remote_peers_and_skips_local(self) -> None:
        # The peer brief renders the REMOTE target names from the LIVE
        # store — local (non-tree) targets are excluded. The static brief
        # text is deliberately NOT pinned byte-for-byte (static prompt
        # content makes a meaningless test); the store-driven dynamics
        # are the contract.
        wiring = await _CAPABILITY.assemble(
            self._binding("subagents.peer"),
            _Topology.chain(_Topology.ROOT, "native_main"),
        )
        store = wiring.artifacts["target_store"]
        store.add(
            CommunicationTarget(
                name="beta",
                kind=AgentCommKind.NORMAL,
                pool_name="pool-beta",
                tree_ref=MagicMock(),
            )
        )
        store.add(
            CommunicationTarget(
                name="gamma",
                kind=AgentCommKind.NORMAL,
                pool_name="pool-gamma",
                tree_ref=MagicMock(),
            )
        )
        store.add(
            CommunicationTarget(
                name="alpha",
                kind=AgentCommKind.NORMAL,
                pool_name="pool-alpha",
            )
        )
        provider = wiring.prompt_providers[0]
        content = await provider.get_or_refresh()

        assert "## Communicating With Remote Agents" in content
        assert "beta" in content
        assert "gamma" in content
        assert "alpha" not in content

    async def test_sections_render_in_binding_order(self) -> None:
        wiring = await _CAPABILITY.assemble(
            self._binding(
                "subagents.delegation",
                "subagents.consultation",
                "subagents.peer",
            ),
            _Topology.chain(_Topology.ROOT, "native_main"),
        )
        assert [type(p).__name__ for p in wiring.prompt_providers] == [
            "_StaticBriefProvider",
            "_StaticBriefProvider",
            "_PeerSectionProvider",
        ]


class TestDarkSupply:
    """A zero-topology pool compiles no capability — no supply, loud tools."""

    def test_lone_root_predicate_is_false(self) -> None:
        view = AgentDeclarationView(
            pool_name="solo",
            agent_name="solo",
            is_root=True,
            parent=None,
            children=(),
            peers=(),
            declared=AgentDeclaredFields(),
        )
        assert _CAPABILITY.applies(view) is False

    async def test_hand_referenced_task_tool_loud_raises(self) -> None:
        # A hand-referenced `task` roster entry on a capability-less pool
        # (the {} capability_supply of a zero-topology pool) cannot
        # resolve — never a silently-empty tool.
        from modex_agent.plugins.assembly.context import AgentContext

        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
            ),
            agent_name="solo",
        )
        with pytest.raises(ValueError, match="'subagents' capability"):
            await TaskToolFactory().create(_empty_config(), ctx)


class TestTopologyGateFaces:
    """The shared pool service + per-sender topology input parity."""

    def _runtime_context(self, agent_name: str, comm_kind: AgentCommKind) -> Any:
        return RuntimeAgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(f"conv1.{agent_name}"),
            comm_kind=comm_kind,
        )

    async def test_tool_passed_children_allow_mid_level_dispatch(self) -> None:
        # A mid-level subagent dispatching its OWN child through the
        # shared pool service: the tool passes the per-sender set (the
        # retired per-agent-service behavior, preserved).
        from unittest.mock import AsyncMock

        from modex_agent.multi_agent.communication.service import AgentCommunicationService

        tree = MagicMock()
        tree.deliver = AsyncMock(return_value=None)
        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            registry=MagicMock(),
            tree=tree,
        )
        target = CommunicationTarget(
            name="leaf",
            kind=AgentCommKind.SUBAGENT,
            pool_name="default",
        )
        result = await service._send(
            target=target,
            content="work",
            invocation_id=None,
            context=self._runtime_context("mid", AgentCommKind.SUBAGENT),
            declared_children=frozenset({"leaf"}),
        )
        assert result.error is None  # topology gate passed; delivery proceeds

    def test_service_level_store_is_the_facade_fallback(self) -> None:
        # Direct callers without a per-sender set (the control facade)
        # fall back to the service-level store — the ROOT's store bound
        # by the root's assemble.
        from modex_agent.multi_agent.communication.service import AgentCommunicationService

        service = AgentCommunicationService(
            source=AgentAddress(name="main"),
            registry=MagicMock(),
            tree=MagicMock(),
        )
        root_store = CommunicationTargetStore()
        root_store.add(
            CommunicationTarget(name="mid", kind=AgentCommKind.SUBAGENT, pool_name="default")
        )
        service.set_target_store(root_store)
        assert service._declared_children() == frozenset({"mid"})


class TestCarrierDeaths:
    """The typed carriers the supply face replaced are gone."""

    def test_pool_runtime_deps_has_no_communication_field(self) -> None:
        fields = {f.name for f in __import__("dataclasses").fields(PoolRuntimeDeps)}
        assert "communication" not in fields
        assert "capability_supply" in fields

    def test_supply_infra_has_no_communication_field(self) -> None:
        fields = {f.name for f in __import__("dataclasses").fields(SupplyInfra)}
        assert "communication" not in fields
        assert "pool_specs" in fields

    def test_communication_facilities_type_is_gone(self) -> None:
        import modex_agent.plugins.assembly.context as context_module

        assert not hasattr(context_module, "CommunicationFacilities")

    def test_biz_communication_assembly_is_gone(self) -> None:
        # The death proof the migration contract names: zero production
        # references in the bot package (tests may construct directly).
        bot_dir = Path(__file__).resolve().parents[3] / "examples" / "bot_project" / "bot"
        hits = []
        for path in bot_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "CommunicationFacilities" in text or "AgentCommunicationService" in text:
                hits.append(str(path))
        assert hits == []


def _empty_config() -> BaseModel:
    from modex_agent.plugins.defaults.communication import _ToolConfig

    return _ToolConfig()
