"""Ticket 04 — context chain carriers: layer fields, lattice, and bridge.

Three frozen carriers generalize :class:`AssemblyContext`'s three-layer
shape into a typed capability boundary (SPEC §3.3):

- ``WorkspaceContext`` — path layout + workspace resource handles
  (incl. the MCP shared handle — AC (d)).
- ``PoolContext`` — pool runtime deps (todo store, terminal manager,
  communication facilities — all inside ``PoolRuntimeDeps``) + llm
  provider.
- ``AgentContext`` — the FULL-CHAIN object: agent identity / parent /
  invocation data / per-agent spec reference, joining all layers via
  multiple inheritance (``AgentContext(WorkspaceContext, PoolContext,
  AssemblyContext)``).

The capability boundary is the subtype lattice itself: the resolver
passes one full-chain object to every factory; a factory declaring a
narrow layer accepts it via subtyping and cannot type-reach other
layers (asserted by the mypy meta-test in
``test_context_chain_mypy.py``).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentFactory, ComponentSlot
from modex_agent.plugins.assembly.context import (
    AgentContext,
    AssemblyContext,
    PoolContext,
    PoolRuntimeDeps,
    WorkspaceContext,
    agent_context_chain,
    resolution_context,
)
from modex_agent.plugins.assembly.native_core import _resolve_single
from modex_agent.plugins.assembly.spec import AssemblySpec
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.paths import WorkspacePaths

# ---------------------------------------------------------------------------
# Field-set contracts (ticket AC (a): layer-appropriate field distribution)
# ---------------------------------------------------------------------------


class TestWorkspaceContextFields:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(WorkspaceContext)
        instance = WorkspaceContext(workspace_ctx=MagicMock())
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.mcp_registry = MagicMock()  # type: ignore[misc]

    def test_field_names_exact(self) -> None:
        expected = {
            "workspace_ctx",
            "workspace_registry",
            "workspace_resources",
            "workspace_spec",
            "mcp_registry",
        }
        actual = {f.name for f in dataclasses.fields(WorkspaceContext)}
        assert actual == expected

    def test_path_layout_and_handles_live_here(self) -> None:
        """Paths (workspace_ctx) and the MCP shared handle are workspace
        layer — AC (d): MCP handle reachable via WorkspaceContext."""
        field_map = {f.name: f for f in dataclasses.fields(WorkspaceContext)}
        assert field_map["workspace_ctx"].default is dataclasses.MISSING
        assert field_map["mcp_registry"].default is None
        assert field_map["workspace_registry"].default is None
        assert field_map["workspace_resources"].default is None
        assert field_map["workspace_spec"].default is None


class TestPoolContextFields:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(PoolContext)
        instance = PoolContext()
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.pool_runtime = PoolRuntimeDeps()  # type: ignore[misc]

    def test_field_names_exact(self) -> None:
        """Pool layer = pool_runtime deps (todo store, terminal manager,
        communication facilities — all inside PoolRuntimeDeps) +
        llm_provider. No workspace fields, no path fields."""
        expected = {"pool_runtime", "llm_provider"}
        actual = {f.name for f in dataclasses.fields(PoolContext)}
        assert actual == expected

    def test_no_workspace_layer_fields(self) -> None:
        """Capability boundary (runtime mirror): a PoolContext-declared
        factory cannot reach workspace-layer fields — the fields do not
        exist on this type at all."""
        pool_fields = {f.name for f in dataclasses.fields(PoolContext)}
        for workspace_field in ("workspace_ctx", "mcp_registry"):
            assert workspace_field not in pool_fields


class TestAgentContextFields:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(AgentContext)
        instance = _full_chain()
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.agent_name = "other"  # type: ignore[misc]

    def test_field_names_exact(self) -> None:
        """Full chain = every layer's fields + the agent layer (identity,
        parent, invocation data, per-agent spec reference)."""
        expected = {
            # global layer (legacy AssemblyContext view)
            "registry",
            # workspace layer
            "workspace_ctx",
            "workspace_registry",
            "workspace_resources",
            "workspace_spec",
            "mcp_registry",
            # pool layer
            "pool_runtime",
            "llm_provider",
            # supply infra (legacy view)
            "infra",
            # agent layer
            "agent_name",
            "parent_session",
            "invocation_id",
            "spec",
            "llm_defaults",
            "capability_wirings",
        }
        actual = {f.name for f in dataclasses.fields(AgentContext)}
        assert actual == expected

    def test_agent_name_required(self) -> None:
        with pytest.raises(TypeError, match="agent_name"):
            AgentContext(registry=MagicMock(), workspace_ctx=MagicMock())

    def test_agent_layer_fields_default_none(self) -> None:
        instance = _full_chain()
        assert instance.parent_session is None
        assert instance.invocation_id is None
        assert instance.spec is None


# ---------------------------------------------------------------------------
# Subtype lattice — the capability boundary mechanism (mechanism (A):
# diamond; the full chain is a subtype of every declarable layer)
# ---------------------------------------------------------------------------


class TestSubtypeLattice:
    def test_agent_context_is_full_chain(self) -> None:
        assert issubclass(AgentContext, WorkspaceContext)
        assert issubclass(AgentContext, PoolContext)
        assert issubclass(AgentContext, AssemblyContext)

    def test_layers_are_isolated_from_each_other(self) -> None:
        assert not issubclass(PoolContext, WorkspaceContext)
        assert not issubclass(WorkspaceContext, PoolContext)
        # The legacy view and the narrow carriers are siblings: a factory
        # declaring PoolContext cannot type-reach AssemblyContext-only
        # fields (registry/infra) and vice versa.
        assert not issubclass(PoolContext, AssemblyContext)
        assert not issubclass(AssemblyContext, PoolContext)
        assert not issubclass(WorkspaceContext, AssemblyContext)
        assert not issubclass(AssemblyContext, WorkspaceContext)

    def test_full_chain_object_satisfies_every_declared_surface(self) -> None:
        chain = _full_chain()
        assert isinstance(chain, WorkspaceContext)
        assert isinstance(chain, PoolContext)
        assert isinstance(chain, AssemblyContext)
        # Legacy-view reads still work on the chain (migration-period two
        # views coexist — BIZ factories keep their AssemblyContext surface).
        assert chain.registry is not None
        assert chain.pool_runtime is not None


# ---------------------------------------------------------------------------
# Bridge — existing AssemblyContext construction flows produce the chain
# (ticket AC: "compat during migration: both views available")
# ---------------------------------------------------------------------------


def _legacy_ctx(
    pool_runtime: PoolRuntimeDeps | None = None,
) -> AssemblyContext:
    return AssemblyContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=pool_runtime,
    )


def _full_chain() -> AgentContext:
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(session_tree_manager=MagicMock()),
        agent_name="probe-agent",
    )


def _spec() -> AssemblySpec:
    workspace_root = Path("/tmp/probe-ws")
    from modex_agent.workspace.context import WorkspaceContext as WorkspaceIdentity

    workspace_ctx = WorkspaceIdentity(
        target=workspace_root,
        paths=WorkspacePaths(root=workspace_root / ".modex"),
        is_home=False,
    )
    return AssemblySpec(
        agent_type="native_main",
        agent_name="probe-agent",
        pool_name="probe-pool",
        tools=[],
        hooks=[],
        llm_provider="default",
        system_prompt_provider="file_prompt",
        system_prompt_config={},
        memory_overrides={},
        execution_strategy="react",
        workspace_ctx=workspace_ctx,
    )


class TestAgentContextChainBridge:
    def test_bridge_carries_all_legacy_view_fields(self) -> None:
        registry = MagicMock()
        workspace_ctx = MagicMock()
        pool_runtime = PoolRuntimeDeps(session_tree_manager=MagicMock())
        ctx = AssemblyContext(
            registry=registry,
            workspace_ctx=workspace_ctx,
            workspace_registry=MagicMock(),
            workspace_resources=MagicMock(),
            pool_runtime=pool_runtime,
            llm_provider=MagicMock(),
            infra=None,
        )

        chain = agent_context_chain(ctx, spec=_spec())

        assert chain.registry is registry
        assert chain.workspace_ctx is workspace_ctx
        assert chain.workspace_registry is ctx.workspace_registry
        assert chain.workspace_resources is ctx.workspace_resources
        assert chain.pool_runtime is pool_runtime
        assert chain.llm_provider is ctx.llm_provider
        assert chain.infra is ctx.infra

    def test_bridge_places_agent_layer_data(self) -> None:
        spec = _spec()
        parent = MagicMock()

        chain = agent_context_chain(
            _legacy_ctx(),
            spec=spec,
            parent_session=parent,
            invocation_id="inv-1",
        )

        assert chain.agent_name == spec.agent_name
        assert chain.spec is spec
        assert chain.parent_session is parent
        assert chain.invocation_id == "inv-1"

    def test_bridge_surfaces_mcp_handle_at_workspace_layer(self) -> None:
        """AC (d): the MCP shared handle is reachable via WorkspaceContext.
        During migration the handle still lives on PoolRuntimeDeps — the
        bridge lifts it to the chain's workspace layer."""
        mcp_registry = MagicMock()
        ctx = _legacy_ctx(PoolRuntimeDeps(mcp_registry=mcp_registry))

        chain = agent_context_chain(ctx, spec=_spec())

        assert chain.mcp_registry is mcp_registry

    def test_bridge_without_pool_runtime_leaves_mcp_none(self) -> None:
        chain = agent_context_chain(_legacy_ctx(None), spec=_spec())
        assert chain.mcp_registry is None

    def test_bridge_product_is_full_chain(self) -> None:
        chain = agent_context_chain(_legacy_ctx(), spec=_spec())
        assert isinstance(chain, AgentContext)
        assert isinstance(chain, AssemblyContext)


class TestLegacyViewUnchanged:
    """The legacy AssemblyContext view keeps its exact shape and helper —
    the two views coexist until the W3 tickets complete."""

    def test_assembly_context_field_set_stable(self) -> None:
        expected = {
            "registry",
            "workspace_ctx",
            "workspace_registry",
            "workspace_resources",
            "workspace_spec",
            "pool_runtime",
            "llm_provider",
            "infra",
        }
        actual = {f.name for f in dataclasses.fields(AssemblyContext)}
        assert actual == expected

    def test_resolution_context_still_builds_legacy_view(self) -> None:
        ctx = resolution_context(
            MagicMock(), MagicMock(), PoolRuntimeDeps(session_tree_manager=MagicMock())
        )
        assert isinstance(ctx, AssemblyContext)
        assert not isinstance(ctx, AgentContext)

    def test_subagent_invocation_context_type_deleted(self) -> None:
        """Ticket 10: the per-invocation special-case type is GONE — its
        semantics live on the AgentContext agent layer (one mechanism)."""
        import modex_agent.plugins.assembly.context as context_module

        assert not hasattr(context_module, "SubagentInvocationContext")


# ---------------------------------------------------------------------------
# Resolver passes the full chain (mechanism (A): pipeline passes the chain;
# narrow factories accept it via subtyping)
# ---------------------------------------------------------------------------


class _ProbeConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class _PoolScopedProbeFactory(ComponentFactory):
    """A PoolContext-declared factory — accepts the full chain via
    subtyping and captures what it received."""

    config_model = _ProbeConfig

    def __init__(self) -> None:
        self.received: PoolContext | None = None

    async def create(
        self,
        config: BaseModel,
        ctx: PoolContext,  # noqa: ARG002
    ) -> object:
        self.received = ctx
        return object()


class TestResolverPassesFullChain:
    async def test_resolve_single_passes_chain_to_narrow_factory(self) -> None:
        registry = ComponentRegistry()
        probe = _PoolScopedProbeFactory()
        registry.register(ComponentSlot.TOOL, "probe", probe)
        chain = _full_chain()

        await _resolve_single(registry, ComponentSlot.TOOL, "probe", {}, chain)

        assert probe.received is chain
        assert isinstance(probe.received, AgentContext)

    async def test_bundled_todo_factory_reads_chain_pool_layer(self) -> None:
        """SPEC §3.3 todo factory example: ``create(config, ctx:
        PoolContext) -> TodoWriteTool(require_todo_supply(pool_runtime)
        .store)`` — the pool's capability supply is the read surface."""
        from modex_agent.plugins.defaults.capabilities.todo import TodoSupply
        from modex_agent.runtime.store import TodoItem, TodoStore
        from modex_agent.tools.standard.todo_tool import TodoWriteTool

        class _Store(TodoStore):
            async def save(self, session_id: str, todos: list[TodoItem]) -> None:
                return None

            async def get(self, session_id: str) -> list[TodoItem]:
                return []

            async def delete(self, session_id: str) -> None:
                return None

        store = _Store()
        registry = ComponentRegistry()
        from modex_agent.plugins.defaults.tools import TodoToolFactory
        from modex_agent.plugins.loader import PluginRegistrationContext

        with PluginRegistrationContext(registry) as registration:
            from modex_agent.plugins.defaults.tools import register_default_tools

            register_default_tools(registration)

        factory = registry.resolve(ComponentSlot.TOOL, "todo_write")
        assert isinstance(factory, TodoToolFactory)
        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(capability_supply={"todo": TodoSupply(store=store)}),
            agent_name="probe-agent",
        )

        tool = await factory.create(factory.config_model(), chain)

        assert isinstance(tool, TodoWriteTool)
        assert tool._store is store  # noqa: SLF001


class TestWorkspaceSpecConsumption:
    """Ticket 14 (AC a): the declared workspace resource selection is
    expressible in YAML and consumed via WorkspaceContext by factories."""

    async def test_factory_reads_declared_selection_through_chain(self) -> None:
        """A WorkspaceContext-declared factory reads the workspace layer's
        declared selection (backend/paths/MCP set) off the chain."""
        from modex_agent.scope.spec import (
            ScopeKind,
            ScopeSpec,
            WorkspacePathsSpec,
            WorkspacePersistenceSpec,
            WorkspaceSpec,
        )

        declared = WorkspaceSpec(
            name="wired",
            persistence=WorkspacePersistenceSpec(backend="sqlite"),
            paths=WorkspacePathsSpec(data_dir_name=".modex"),
            mcp=["playwright"],
        )
        spec = ScopeSpec(kind=ScopeKind.WORKSPACE, workspace=declared)

        received: list[WorkspaceSpec | None] = []

        class _WorkspaceScopedProbeFactory(ComponentFactory):
            config_model = _ProbeConfig

            async def create(
                self,
                config: BaseModel,
                ctx: WorkspaceContext,
            ) -> object:
                received.append(ctx.workspace_spec)
                return object()

        registry = ComponentRegistry()
        registry.register(ComponentSlot.TOOL, "ws_probe", _WorkspaceScopedProbeFactory())
        chain = AgentContext(
            registry=registry,
            workspace_ctx=MagicMock(),
            workspace_spec=spec.workspace,
            agent_name="probe-agent",
        )

        await _resolve_single(registry, ComponentSlot.TOOL, "ws_probe", {}, chain)

        assert received == [declared]

    def test_bridge_passes_workspace_spec_through(self) -> None:
        from modex_agent.scope.spec import ScopeKind, ScopeSpec, WorkspaceSpec

        declared = WorkspaceSpec(name="bridged")
        ctx = AssemblyContext(
            registry=MagicMock(),
            workspace_ctx=MagicMock(),
            workspace_spec=ScopeSpec(kind=ScopeKind.WORKSPACE, workspace=declared).workspace,
        )

        chain = agent_context_chain(ctx, spec=_spec())

        assert chain.workspace_spec is declared
