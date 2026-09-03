"""Nested-tree communication assembly (ticket 12, SPEC §3.2/§5.2).

The mid-level agent of a three-level declared tree gets a per-agent
``CommunicationTargetStore`` holding exactly its DIRECT children (never
grandchildren); the store is built by the ``subagents`` capability's
assemble from the DECLARED pool tree, and the derived ``task`` TOOL-slot
factory resolves against it (the pool's capability supply carries the
service). A leaf gets no store at all — its assembly carries no ``task``
entry, and the factory fails loudly if one is requested.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.tools import TaskDispatchTool
from modex_agent.plugins.assembly.context import (
    AgentContext,
    PoolRuntimeDeps,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.capability import CapabilityBinding
from modex_agent.plugins.defaults.capabilities.subagents import (
    SubagentsCapability,
    SubagentsSupply,
)
from modex_agent.plugins.defaults.communication import TaskToolFactory
from modex_agent.scope.spec import AgentSpec, PoolSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_ROOT = AgentSpec(name="root", description="root agent")
_MID = AgentSpec(name="mid", description="middle agent", parent="root")
_LEAF = AgentSpec(name="leaf", description="leaf agent", parent="mid")
_LEAF2 = AgentSpec(name="leaf2", description="", parent="mid")
_GRANDCHILD = AgentSpec(name="subsub", description="grandchild agent", parent="leaf")

_POOL_SPEC = PoolSpec(
    name="nested",
    agents=[_ROOT, _MID, _LEAF, _LEAF2, _GRANDCHILD],
)

_CAPABILITY = SubagentsCapability()


def _pool_assembly() -> PoolAssemblyContext:
    return PoolAssemblyContext(
        pool_name="nested",
        pool_spec=_POOL_SPEC,
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


def _spec(agent_name: str, agent_type: str = "native_sub") -> AssemblySpec:
    """A minimal compiled spec for one agent of the tree."""
    return AssemblySpec(
        agent_name=agent_name,
        pool_name="nested",
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


def _chain(agent_name: str, *, supply: SubagentsSupply | None = None) -> AgentContext:
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=WorkspaceContext(
            target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
        ),
        pool_runtime=PoolRuntimeDeps(
            pool_assembly_ctx=_pool_assembly(),
            capability_supply={"subagents": supply or SubagentsSupply(service=_mock_service())},
        ),
        agent_name=agent_name,
        spec=_spec(agent_name),
    )


def _mock_service() -> MagicMock:
    service = MagicMock(spec=AgentCommunicationService)
    return service


async def _assemble_store(agent_name: str) -> Any:
    wiring = await _CAPABILITY.assemble(CapabilityBinding(), _chain(agent_name))
    return wiring.artifacts.get("target_store")


class TestMidLevelStore:
    async def test_mid_store_lists_direct_children_only(self) -> None:
        # The mid's store holds exactly its DIRECT children; the
        # grandchild (the leaf's own dispatch surface) never joins.
        store = await _assemble_store("mid")
        assert store is not None
        assert [t.name for t in store.list_subagents()] == ["leaf", "leaf2"]
        assert all(t.kind == AgentCommKind.SUBAGENT for t in store.list_subagents())

    async def test_childless_agent_has_no_target_store(self) -> None:
        # No store: the derived send_to_agent entry builds its own
        # subagent-mode store; there is no task entry to resolve. (``leaf``
        # itself is child-carrying — [subsub] — the three-level tree's
        # second mid level; ``leaf2`` is the true leaf.)
        assert await _assemble_store("leaf2") is None

    async def test_root_store_always_builds_even_without_children(self) -> None:
        # The pool ROOT always gets a store (possibly empty — peer NORMAL
        # targets join it at workspace materialize); a root with children
        # lists them as SUBAGENT entries.
        supply_service = _mock_service()
        supply = SubagentsSupply(service=supply_service)
        chain = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
            ),
            pool_runtime=PoolRuntimeDeps(
                pool_assembly_ctx=_pool_assembly(),
                capability_supply={"subagents": supply},
            ),
            agent_name="root",
            spec=_spec("root", agent_type="native_main"),
        )
        wiring = await _CAPABILITY.assemble(CapabilityBinding(), chain)
        store = wiring.artifacts.get("target_store")
        assert store is not None
        assert [t.name for t in store.list_subagents()] == ["mid"]
        # The root's assembly binds the SAME store onto the pool service
        # (the topology-gate fallback carrier for direct callers).
        supply_service.set_target_store.assert_called_once_with(store)


class TestTaskToolFactory:
    async def test_resolves_mid_store_from_wiring_artifacts(self) -> None:
        # Given — the mid's wiring (store from the declared tree) on the
        # chain + the factory the compiled `task` entry resolves through
        wiring = await _CAPABILITY.assemble(CapabilityBinding(), _chain("mid"))
        chain = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
            ),
            pool_runtime=PoolRuntimeDeps(
                capability_supply={"subagents": SubagentsSupply(service=MagicMock())}
            ),
            agent_name="mid",
            capability_wirings={"subagents": wiring},
        )

        # When
        tool = await TaskToolFactory().create(_empty_config(), chain)

        # Then — the tool's target list IS the direct-children store
        assert isinstance(tool, TaskDispatchTool)
        assert tool.name == "task"
        assert [t.name for t in tool.list_targets()] == ["leaf", "leaf2"]

    async def test_fails_loudly_without_store_artifact(self) -> None:
        # Given — wiring without a store (a childless agent's) but a
        # derived `task` entry requesting resolution
        wiring = await _CAPABILITY.assemble(CapabilityBinding(), _chain("leaf2"))
        chain = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
            ),
            pool_runtime=PoolRuntimeDeps(
                capability_supply={"subagents": SubagentsSupply(service=_mock_service())}
            ),
            agent_name="leaf2",
            capability_wirings={"subagents": wiring},
        )

        # When / Then — loud, not a silently-empty tool
        with pytest.raises(ValueError, match="per-agent target store"):
            await TaskToolFactory().create(_empty_config(), chain)

    async def test_fails_loudly_without_supply(self) -> None:
        # Given — no subagents capability supply on the chain at all
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(),
                paths=WorkspacePaths(root=MagicMock()),
                is_home=False,
            ),
            agent_name="mid",
        )

        # When / Then
        with pytest.raises(ValueError, match="'subagents' capability"):
            await TaskToolFactory().create(_empty_config(), ctx)

    async def test_fails_loudly_with_wrong_typed_supply(self) -> None:
        # Given — a foreign object under the subagents key
        chain = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
            ),
            pool_runtime=PoolRuntimeDeps(capability_supply={"subagents": object()}),  # type: ignore[dict-item]
            agent_name="mid",
        )

        with pytest.raises(ValueError, match="must be SubagentsSupply"):
            await TaskToolFactory().create(_empty_config(), chain)


def _empty_config() -> Any:
    from modex_agent.plugins.defaults.communication import _ToolConfig

    return _ToolConfig()
