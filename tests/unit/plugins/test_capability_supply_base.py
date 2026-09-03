"""TDD tests for the capability supply base (task 4, capability-bundles).

Pool-level supply face (SPEC §7.1): ONE aggregation per pool over the
pool's effective capabilities lands on ``PoolRuntimeDeps.capability_supply``
(main-agent path, Stage 3) and is threaded through
``AgentMaterializeDeps.capability_supply`` into subagent materialization.
No consumer migrates here — this suite pins the PIPELINE only.

Stage-level tests mirror the ``test_stage_pool.py`` harness shapes (stub
execution strategy + supply-mode ``SupplyInfra``). Every capability below
is a test double — DefaultPlugin registers NO capabilities.

Covers:

- (a) empty-capability pool → ``capability_supply == {}``
- (b) two agents effective on the same capability → ``supply()`` built
  EXACTLY ONCE, the PoolSupplyView carrying both agent entries
- (b2) subagent-only capability → still aggregated (``pool_specs``
  threading makes the stage see the whole pool)
- (c) ``supply()`` raising → pool assembly aborts loudly, no partial state
- (d) ``supply()`` returning None → no mapping entry, no error
- (e) subagent materialize → the chain's ``pool_runtime.capability_supply``
  carries the deps mapping (identity — one mapping object, both faces)
- (f) determinism — same specs + registry → same aggregation key order
  across two runs (first-appearance order, not sorted order)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.plugins.abc import ComponentFactory, ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.capability_supply import (
    assemble_capability_supplies,
    stop_capability_supplies,
)
from modex_agent.plugins.assembly.context import AssemblyContext, SupplyInfra
from modex_agent.plugins.assembly.pipeline import AssemblyPipeline, AssemblyStage
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.assembly.stages.infra_assemble import InfraAssembleStage
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilitySupply,
    CapabilityWiring,
    CompiledCapability,
    PoolSupplyAgentEntry,
    PoolSupplyView,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# ─── Stage harness (shapes mirrored from test_stage_pool.py) ─────────────────


class _StubStrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _StubExecutionStrategy(ExecutionStrategy):
    """Real ExecutionStrategy subclass; ``assemble_main`` swapped for AsyncMock."""

    @property
    def name(self) -> str:
        return "stub"

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        return StrategyAssembly()  # pragma: no cover — replaced by AsyncMock

    def validate_pool_spec(self, pool: Any) -> None:  # noqa: ARG002
        pass


def _make_stub_strategy() -> _StubExecutionStrategy:
    strategy = _StubExecutionStrategy()
    strategy.assemble_main = AsyncMock(return_value=MagicMock(spec=StrategyAssembly))  # type: ignore[method-assign]
    return strategy


def _make_workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_capability_supply_ws")
    return WorkspaceContext(
        target=target,
        paths=WorkspacePaths(root=target),
        is_home=False,
    )


def _make_spec(
    agent_name: str = "test_agent",
    *,
    capabilities: tuple[CompiledCapability, ...] = (),
) -> AssemblySpec:
    return AssemblySpec(
        agent_type="native_main",  # type: ignore[arg-type]
        agent_name=agent_name,
        pool_name="test_pool",
        tools=[],
        hooks=[],
        llm_provider="test",
        system_prompt_provider="test",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="stub",
        capabilities=capabilities,
        workspace_ctx=_make_workspace_ctx(),
    )


def _compiled(name: str, config: dict[str, Any]) -> CompiledCapability:
    return CompiledCapability(name=name, config=config, binding=CapabilityBinding())


def _make_pool_assembly_ctx() -> PoolAssemblyContext:
    return PoolAssemblyContext(
        pool_name="test_pool",
        pool_spec=MagicMock(),
        project_dir=Path("/tmp/test_project"),
        data_dir=Path("/tmp/test_data"),
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
    )


def _make_registry(
    stub_strategy: ExecutionStrategy,
    capabilities: tuple[Capability, ...] = (),
) -> ComponentRegistry:
    registry = ComponentRegistry()
    registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "stub",
        SimpleFactory(stub_strategy, _StubStrategyConfig),
    )
    if capabilities:
        ctx = PluginRegistrationContext(registry)
        for capability in capabilities:
            ctx.register_capability(capability.name, capability)
        ctx.flush()
    return registry


def _make_supply(
    pool_specs: tuple[AssemblySpec, ...] = (),
) -> SupplyInfra:
    return SupplyInfra(
        pool_assembly_ctx=_make_pool_assembly_ctx(),
        pool=MagicMock(spec=AgentPool),
        pool_specs=pool_specs,
    )


def _make_assembly_ctx(
    registry: ComponentRegistry,
    infra: SupplyInfra,
) -> AssemblyContext:
    return AssemblyContext(
        registry=registry,
        workspace_ctx=_make_workspace_ctx(),
        infra=infra,
    )


# ─── Capability test doubles ─────────────────────────────────────────────────


class _MarkerSupply(CapabilitySupply):
    """Concrete supply object — capability-specific shape."""

    def __init__(self, label: str) -> None:
        self.label = label


class _SupplyCapability(Capability):
    """Records every supply() call; returns a fixed product."""

    def __init__(self, name: str = "dummy") -> None:
        self.name = name
        self.product = _MarkerSupply(name)
        self.supply_views: list[PoolSupplyView] = []

    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        self.supply_views.append(view)
        return self.product

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _QuietCapability(Capability):
    """Default supply() → None — no pool-level supply, no error."""

    name = "quiet"

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _ExplodingCapability(Capability):
    """supply() raises — pool assembly must abort loudly."""

    name = "boom"

    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        raise ValueError("supply exploded")

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _LifecycleSupply(CapabilitySupply):
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError(f"start failed: {self.name}")

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError(f"stop failed: {self.name}")


class _LifecycleCapability(Capability):
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_construction: bool = False,
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.fail_construction = fail_construction
        self.product = _LifecycleSupply(
            name,
            events,
            fail_start=fail_start,
            fail_stop=fail_stop,
        )

    def supply(self, view: PoolSupplyView) -> CapabilitySupply | None:
        self.events.append(f"construct:{self.name}")
        if self.fail_construction:
            raise RuntimeError(f"construction failed: {self.name}")
        return self.product

    async def assemble(self, binding: CapabilityBinding, ctx: object) -> CapabilityWiring:
        return CapabilityWiring()


class _NoopStage(AssemblyStage):
    async def process(
        self,
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        ctx: AssemblyContext,
    ) -> None:
        pass


def _supply_view() -> PoolSupplyView:
    return PoolSupplyView(pool_name="test_pool", entries=())


class TestAtomicCapabilitySupplyLifecycle:
    async def test_success_returns_immutable_mapping_and_ordered_handles(self) -> None:
        events: list[str] = []
        first = _LifecycleCapability("first", events)
        second = _LifecycleCapability("second", events)
        registry = _make_registry(_make_stub_strategy(), (first, second))
        spec = _make_spec(
            capabilities=(_compiled("first", {}), _compiled("second", {}))
        )

        mapping, handles = await assemble_capability_supplies(
            (spec,), registry, _supply_view()
        )

        assert list(mapping) == ["first", "second"]
        assert handles == (first.product, second.product)
        assert events == [
            "construct:first",
            "construct:second",
            "start:first",
            "start:second",
        ]
        with pytest.raises(TypeError):
            cast(dict[str, CapabilitySupply], mapping)["other"] = first.product
        await stop_capability_supplies(handles)

    async def test_later_construction_failure_stops_every_constructed_product(self) -> None:
        events: list[str] = []
        first = _LifecycleCapability("first", events)
        second = _LifecycleCapability("second", events, fail_stop=True)
        third = _LifecycleCapability("third", events, fail_construction=True)
        registry = _make_registry(_make_stub_strategy(), (first, second, third))
        spec = _make_spec(
            capabilities=(
                _compiled("first", {}),
                _compiled("second", {}),
                _compiled("third", {}),
            )
        )

        with pytest.raises(RuntimeError, match="construction failed: third"):
            await assemble_capability_supplies((spec,), registry, _supply_view())

        assert events == [
            "construct:first",
            "construct:second",
            "construct:third",
            "stop:second",
            "stop:first",
        ]

    async def test_later_start_failure_stops_all_products_in_reverse_with_isolation(
        self,
    ) -> None:
        events: list[str] = []
        first = _LifecycleCapability("first", events)
        second = _LifecycleCapability("second", events, fail_stop=True)
        third = _LifecycleCapability("third", events, fail_start=True)
        registry = _make_registry(_make_stub_strategy(), (first, second, third))
        spec = _make_spec(
            capabilities=(
                _compiled("first", {}),
                _compiled("second", {}),
                _compiled("third", {}),
            )
        )

        with pytest.raises(RuntimeError, match="start failed: third"):
            await assemble_capability_supplies((spec,), registry, _supply_view())

        assert events == [
            "construct:first",
            "construct:second",
            "construct:third",
            "start:first",
            "start:second",
            "start:third",
            "stop:third",
            "stop:second",
            "stop:first",
        ]

    async def test_pool_pipeline_failure_stops_started_supply(self) -> None:
        events: list[str] = []
        capability = _LifecycleCapability("lifecycle", events)
        strategy = _make_stub_strategy()
        strategy.assemble_main.side_effect = RuntimeError("strategy failed")  # type: ignore[attr-defined]
        registry = _make_registry(strategy, (capability,))
        spec = _make_spec(capabilities=(_compiled("lifecycle", {}),))
        infra = _make_supply()
        pipeline = AssemblyPipeline(
            workspace_materialize=_NoopStage(),
            infra_assemble=InfraAssembleStage(),
            pool_assemble=PoolAssembleStage(),
            agent_assemble=_NoopStage(),
        )

        with pytest.raises(RuntimeError, match="strategy failed"):
            await pipeline.run(spec, _make_assembly_ctx(registry, infra))

        assert events == [
            "construct:lifecycle",
            "start:lifecycle",
            "stop:lifecycle",
        ]


# ─── (a) empty-capability pool ───────────────────────────────────────────────


class TestEmptyCapabilityPool:
    async def test_no_capabilities_yields_empty_supply_mapping(self) -> None:
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.infra = _make_supply()
        ctx = _make_assembly_ctx(registry, builder.infra)

        await PoolAssembleStage().process(_make_spec(), builder, ctx)

        propagated = builder.propagated_context
        assert propagated is not None
        assert propagated.pool_runtime is not None
        assert propagated.pool_runtime.capability_supply == {}


# ─── (b) one capability, two agents → supply built exactly once ─────────────


class TestSingleAggregationPerPool:
    async def test_two_agents_one_capability_supply_built_once(self) -> None:
        capability = _SupplyCapability("dummy")
        stub = _make_stub_strategy()
        registry = _make_registry(stub, (capability,))
        main_spec = _make_spec(
            "main_agent", capabilities=(_compiled("dummy", {"n": 1}),)
        )
        sub_spec = _make_spec("sub_agent", capabilities=(_compiled("dummy", {"n": 2}),))
        builder = AssemblyBuilder()
        builder.infra = _make_supply(pool_specs=(main_spec, sub_spec))
        ctx = _make_assembly_ctx(registry, builder.infra)

        await PoolAssembleStage().process(main_spec, builder, ctx)

        # EXACTLY ONE supply() call for the whole pool.
        assert len(capability.supply_views) == 1
        view = capability.supply_views[0]
        assert view.pool_name == "test_pool"
        assert view.entries == (
            PoolSupplyAgentEntry(agent_name="main_agent", config={"n": 1}),
            PoolSupplyAgentEntry(agent_name="sub_agent", config={"n": 2}),
        )
        mapping = builder.propagated_context.pool_runtime.capability_supply
        assert list(mapping) == ["dummy"]
        assert mapping["dummy"] is capability.product

    async def test_subagent_only_capability_still_aggregated(self) -> None:
        """pool_specs threading: a capability effective ONLY on a subagent
        still gets its pool-level supply — the stage sees the whole pool,
        not just the pipeline input spec."""
        capability = _SupplyCapability("dummy")
        stub = _make_stub_strategy()
        registry = _make_registry(stub, (capability,))
        main_spec = _make_spec("main_agent")
        sub_spec = _make_spec("sub_agent", capabilities=(_compiled("dummy", {}),))
        builder = AssemblyBuilder()
        builder.infra = _make_supply(pool_specs=(main_spec, sub_spec))
        ctx = _make_assembly_ctx(registry, builder.infra)

        await PoolAssembleStage().process(main_spec, builder, ctx)

        mapping = builder.propagated_context.pool_runtime.capability_supply
        assert list(mapping) == ["dummy"]
        assert capability.supply_views[0].entries == (
            PoolSupplyAgentEntry(agent_name="sub_agent", config={}),
        )


# ─── (c) supply() raising aborts pool assembly ──────────────────────────────


class TestSupplyFailureAborts:
    async def test_supply_raise_propagates_no_partial_state(self) -> None:
        capability = _ExplodingCapability()
        stub = _make_stub_strategy()
        registry = _make_registry(stub, (capability,))
        spec = _make_spec(capabilities=(_compiled("boom", {}),))
        builder = AssemblyBuilder()
        builder.infra = _make_supply()
        ctx = _make_assembly_ctx(registry, builder.infra)

        with pytest.raises(ValueError, match="supply exploded"):
            await PoolAssembleStage().process(spec, builder, ctx)

        # No partial supply landed on the builder.
        assert builder.propagated_context is None
        assert builder.strategy_result is None


# ─── (d) supply() returning None → no entry, no error ────────────────────────


class TestNoneSupplySkipped:
    async def test_none_return_yields_no_entry(self) -> None:
        capability = _QuietCapability()
        stub = _make_stub_strategy()
        registry = _make_registry(stub, (capability,))
        spec = _make_spec(capabilities=(_compiled("quiet", {}),))
        builder = AssemblyBuilder()
        builder.infra = _make_supply()
        ctx = _make_assembly_ctx(registry, builder.infra)

        await PoolAssembleStage().process(spec, builder, ctx)

        assert builder.propagated_context.pool_runtime.capability_supply == {}


# ─── (f) determinism ─────────────────────────────────────────────────────────


class TestAggregationDeterminism:
    async def test_same_specs_registry_same_key_order_across_runs(self) -> None:
        """First-appearance order (zeta before alpha), NOT sorted order —
        and identical across two independent runs."""
        capabilities = (
            _SupplyCapability("zeta"),
            _SupplyCapability("alpha"),
            _SupplyCapability("mid"),
        )
        stub = _make_stub_strategy()
        registry = _make_registry(stub, capabilities)
        main_spec = _make_spec(
            "main_agent",
            capabilities=(_compiled("zeta", {}), _compiled("alpha", {})),
        )
        sub_spec = _make_spec(
            "sub_agent", capabilities=(_compiled("mid", {}), _compiled("zeta", {}))
        )

        mappings = []
        for _ in range(2):
            builder = AssemblyBuilder()
            builder.infra = _make_supply(pool_specs=(main_spec, sub_spec))
            ctx = _make_assembly_ctx(registry, builder.infra)
            await PoolAssembleStage().process(main_spec, builder, ctx)
            mappings.append(
                builder.propagated_context.pool_runtime.capability_supply
            )

        assert list(mappings[0]) == ["zeta", "alpha", "mid"]
        assert list(mappings[0]) == list(mappings[1])


# ─── (e) subagent materialize threading ──────────────────────────────────────


class _ProbeToolConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _CapturingToolFactory(ComponentFactory):
    """TOOL-slot factory capturing the ctx each dispatch receives."""

    config_model = _ProbeToolConfig

    def __init__(self) -> None:
        self.captured: list[Any] = []

    async def create(self, config: BaseModel, ctx: Any) -> Any:
        self.captured.append(ctx)
        return MagicMock(name="probe_tool")


class TestSubagentMaterializeThreading:
    async def test_materialize_chain_carries_deps_capability_supply(self) -> None:
        """After AgentTemplate.materialize, the assembly-time context chain's
        pool_runtime.capability_supply IS the deps mapping — the subagent
        path receives the same pool-wide face Stage 3 built."""
        from modex_agent.core.session_id import SessionIdFactory
        from modex_agent.multi_agent.context_fork import ContextForkBuilder
        from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
        from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
        from modex_agent.multi_agent.template import AgentTemplate
        from modex_agent.plugins.defaults import DefaultPlugin
        from modex_agent.plugins.loader import (
            ComponentRegistryLoader,
            PluginDiscoveryConfig,
        )
        from modex_agent.scope.compiler import compile_scope
        from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
        from modex_agent.workspace.scope_path import ScopePath

        registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(),
            ),
        )
        probe = _CapturingToolFactory()
        registry.register(ComponentSlot.TOOL, "probe_tool", probe)

        # Wholesale tools list → the compiled roster is exactly [probe_tool],
        # so the probe factory is the only TOOL dispatch during materialize.
        declared = AgentSpec(
            name="scout", parent="main", tools=["probe_tool"]
        )
        scope = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(name="main", agents=[AgentSpec(name="main"), declared]),
        )
        compilation = compile_scope(scope, workspace_ctx=_make_workspace_ctx())
        compiled = next(
            agent for agent in compilation.agents if agent.provenance.agent == "scout"
        )
        template = AgentTemplate(
            spec=declared,
            toolset_profile=compiled.defaults.toolset_profile,
            compiled_spec=compiled.spec,
        )

        supply = _MarkerSupply("dummy")
        supply_map = {"dummy": supply}

        fake_instance = MagicMock()
        fake_instance.pipeline = MagicMock()
        fake_instance.stop = AsyncMock()
        pool = MagicMock()
        pool.register_resident = AsyncMock()
        factory = MagicMock()
        factory.create_agent = AsyncMock(return_value=fake_instance)

        deps = AgentMaterializeDeps(
            agent_factory=factory,
            pool=pool,
            session_factory=SessionIdFactory(),
            broker=MagicMock(),
            tree=MagicMock(spec=SessionTreeManager),
            llm_provider=MagicMock(),
            project_dir=None,  # skip prompt file read + MCP + skills
            component_registry=registry,
            capability_supply=supply_map,
        )
        deps.context_fork_builder = ContextForkBuilder()
        deps.scope_path = ScopePath(workspace_root=Path("/ws"), pool_name="main")

        parent = SessionIdFactory().create(agent_name="main")
        await template.materialize(
            parent_session=parent, invocation_id="inv1", deps=deps
        )

        assert probe.captured, "probe TOOL factory must have been dispatched"
        for chain_ctx in probe.captured:
            pool_runtime = chain_ctx.pool_runtime
            assert pool_runtime is not None
            assert pool_runtime.capability_supply is supply_map
            assert pool_runtime.capability_supply["dummy"] is supply
