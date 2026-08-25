"""TDD tests for PoolAssembleStage — pool assembly pipeline stage 3.

Asserts (SPEC section 6.3, stage 3):

- ``PoolAssembleStage`` is an ``AssemblyStage`` subclass with async
  ``process()``.
- ``process()`` resolves the ``EXECUTION_STRATEGY`` factory by name from
  ``ctx.registry`` and awaits ``factory.create(config, ctx)``.
- ``process()`` awaits ``strategy.assemble_main(pool_assembly_ctx)`` — verified
  via ``AsyncMock.assert_awaited()`` (not just "called").
- ``process()`` creates an :class:`AgentPool` and sets ``builder.pool``.
- ``process()`` creates an :class:`AgentDescriptor` and sets
  ``builder.descriptor``.
- ``process()`` sets ``builder.strategy_result`` to the
  :class:`StrategyAssembly` returned by ``strategy.assemble_main``.
- The stub strategy is a real :class:`ExecutionStrategy` subclass (ABC
  contract verified by class definition, not a mock).

Contract correction from plan: the strategy returns ``StrategyAssembly``
only (turn runner / pipeline / collaborators). The stage — NOT the
strategy — creates the :class:`AgentPool` and :class:`AgentDescriptor`.
"""
from __future__ import annotations

import inspect
from abc import ABC
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.multi_agent.descriptor import AgentDescriptor
from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    PoolAssemblyContext,
    StrategyAssembly,
)
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    PoolRuntimeDeps,
    SupplyInfra,
)
from modex_agent.plugins.assembly.pipeline import AssemblyStage
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# ─── Stub strategy ───────────────────────────────────────────────────────────


class _StubStrategyConfig(BaseModel):
    """Empty frozen config for the stub execution strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _StubExecutionStrategy(ExecutionStrategy):
    """Real ExecutionStrategy subclass — verifies the ABC contract.

    The ``assemble`` method is replaced on the instance with an
    :class:`AsyncMock` so the test can call ``assert_awaited()``. The
    class still defines ``async def assemble_main`` (satisfying the ABC at
    class-definition time), but the instance method is swapped to a
    tracking mock after construction.
    """

    @property
    def name(self) -> str:
        return "stub"

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        return StrategyAssembly()  # pragma: no cover — replaced by AsyncMock

    def validate_pool_spec(self, spec: Any) -> None:  # noqa: ARG002
        pass


def _make_stub_strategy() -> _StubExecutionStrategy:
    """Create a stub strategy with ``assemble`` replaced by an AsyncMock.

    Returns the strategy instance; the mock assembly returned by
    ``assemble`` is stored on ``strategy._mock_assembly`` for assertions.
    """
    strategy = _StubExecutionStrategy()
    mock_assembly = MagicMock(spec=StrategyAssembly)
    strategy.assemble_main = AsyncMock(return_value=mock_assembly)  # type: ignore[method-assign]
    strategy._mock_assembly = mock_assembly  # type: ignore[attr-defined]
    return strategy


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _make_workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_stage_pool_ws")
    return WorkspaceContext(
        target=target,
        paths=WorkspacePaths(root=target),
        is_home=False,
    )


def _make_spec() -> AssemblySpec:
    return AssemblySpec(
        agent_type="native_main",  # type: ignore[arg-type]
        agent_name="test_agent",
        pool_name="test_pool",
        tools=[],
        hooks=[],
        llm_provider="test",
        system_prompt_provider="test",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="stub",
        workspace_ctx=_make_workspace_ctx(),
    )


def _make_workspace_resources() -> SimpleNamespace:
    """Stub workspace_resources — object with attributes the stage reads."""
    return SimpleNamespace(
        pool_spec=MagicMock(),
        project_dir=Path("/tmp/test_project"),
        data_dir=Path("/tmp/test_data"),
        pool_data=MagicMock(),
        workspace_handle=MagicMock(),
        workspace_resolver=MagicMock(),
    )


def _make_pool_assembly_ctx() -> PoolAssemblyContext:
    """Build a PoolAssemblyContext with mock deps — for supply-mode tests."""
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


def _make_supply() -> SupplyInfra:
    """Supply infra — the production shape create_pool hands the pipeline."""
    return SupplyInfra(
        pool_assembly_ctx=_make_pool_assembly_ctx(),
        pool=MagicMock(spec=AgentPool),
    )


def _make_registry(stub_strategy: _StubExecutionStrategy) -> ComponentRegistry:
    """ComponentRegistry with the stub strategy registered under 'stub'."""
    registry = ComponentRegistry()
    factory = SimpleFactory(stub_strategy, _StubStrategyConfig)
    registry.register(ComponentSlot.EXECUTION_STRATEGY, "stub", factory)
    return registry


def _make_ctx(
    registry: ComponentRegistry,
    workspace_resources: Any = None,
    infra: SupplyInfra | None = None,
) -> AssemblyContext:
    return AssemblyContext(
        registry=registry,
        workspace_registry=MagicMock(),  # type: ignore[arg-type]
        workspace_ctx=_make_workspace_ctx(),
        workspace_resources=workspace_resources,
        pool_runtime=None,
    )


# ─── ABC contract ────────────────────────────────────────────────────────────


class TestPoolAssembleStageABC:
    def test_is_assembly_stage_subclass(self) -> None:
        assert issubclass(PoolAssembleStage, AssemblyStage)

    def test_is_abc_subclass(self) -> None:
        assert issubclass(PoolAssembleStage, ABC)

    def test_process_is_async(self) -> None:
        assert inspect.iscoroutinefunction(PoolAssembleStage.process)

    def test_process_signature(self) -> None:
        sig = inspect.signature(PoolAssembleStage.process)
        params = list(sig.parameters)
        assert params == ["self", "spec", "builder", "ctx"]


# ─── Process: strategy resolution + assemble ────────────────────────────────


class TestStrategyResolutionAndAssemble:
    """Verify the stage resolves the strategy by name and awaits assemble."""

    async def test_strategy_assemble_is_awaited(self) -> None:
        """The stage must AWAIT strategy.assemble_main — not just call it.

        Verified via ``AsyncMock.assert_awaited()``. A bare coroutine
        call (without await) would return a coroutine object but never
        execute the body; ``assert_awaited`` proves the await happened.
        """
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        stub.assemble_main.assert_awaited()  # type: ignore[attr-defined]
        stub.assemble_main.assert_awaited_once()  # type: ignore[attr-defined]

    async def test_strategy_runtime_outputs_are_propagated(self) -> None:
        stub = _make_stub_strategy()
        terminal_manager = MagicMock()
        todo_store = MagicMock()
        root_provider = MagicMock()
        stub._mock_assembly.terminal_manager = terminal_manager  # type: ignore[attr-defined]
        stub._mock_assembly.todo_store = todo_store  # type: ignore[attr-defined]
        stub._mock_assembly.root_provider = root_provider  # type: ignore[attr-defined]
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()

        await PoolAssembleStage().process(_make_spec(), builder, _make_ctx(registry))

        propagated = builder.propagated_context
        assert propagated is not None
        assert propagated.pool_runtime is not None
        assert propagated.pool_runtime.terminal_manager is terminal_manager
        assert propagated.pool_runtime.todo_store is todo_store
        assert propagated.pool_runtime.root_provider is root_provider

    async def test_strategy_process_registry_is_propagated(self) -> None:
        """Split-brain fix: the strategy's ProcessRegistry must reach
        pool_runtime verbatim so the FW tool factories resolve bash
        against the SAME registry the BIZ terminal trio uses."""
        from modex_agent.tools.terminal import ProcessRegistry

        stub = _make_stub_strategy()
        process_registry = ProcessRegistry()
        stub._mock_assembly.terminal_manager = MagicMock()  # type: ignore[attr-defined]
        stub._mock_assembly.process_registry = process_registry  # type: ignore[attr-defined]
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()

        await PoolAssembleStage().process(_make_spec(), builder, _make_ctx(registry))

        propagated = builder.propagated_context
        assert propagated is not None
        assert propagated.pool_runtime is not None
        assert propagated.pool_runtime.process_registry is process_registry

    async def test_terminal_manager_without_registry_gets_fallback(self) -> None:
        """Invariant: terminal_manager is not None ⇒ process_registry is not
        None. A strategy supplying only a manager gets a fresh registry —
        the half-state is impossible."""
        from modex_agent.tools.terminal import ProcessRegistry

        stub = _make_stub_strategy()
        stub._mock_assembly.terminal_manager = MagicMock()  # type: ignore[attr-defined]
        stub._mock_assembly.process_registry = None  # type: ignore[attr-defined]
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()

        await PoolAssembleStage().process(_make_spec(), builder, _make_ctx(registry))

        propagated = builder.propagated_context
        assert propagated is not None
        assert propagated.pool_runtime is not None
        assert isinstance(propagated.pool_runtime.process_registry, ProcessRegistry)

    async def test_no_terminal_manager_leaves_registry_none(self) -> None:
        stub = _make_stub_strategy()
        stub._mock_assembly.terminal_manager = None  # type: ignore[attr-defined]
        stub._mock_assembly.process_registry = None  # type: ignore[attr-defined]
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()

        await PoolAssembleStage().process(_make_spec(), builder, _make_ctx(registry))

        propagated = builder.propagated_context
        assert propagated is not None
        assert propagated.pool_runtime is not None
        assert propagated.pool_runtime.process_registry is None

    async def test_strategy_assemble_called_with_pool_assembly_ctx(self) -> None:
        """``strategy.assemble_main`` receives a :class:`PoolAssemblyContext`."""
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        call_args = stub.assemble_main.call_args  # type: ignore[attr-defined]
        passed_ctx = call_args.args[0]
        assert isinstance(passed_ctx, PoolAssemblyContext)
        assert passed_ctx.pool_name == "test_pool"

    async def test_factory_create_is_awaited(self) -> None:
        """The stage awaits ``factory.create(config, ctx)`` to get the strategy."""
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        # The strategy returned by factory.create is the same stub —
        # if factory.create weren't awaited, strategy.assemble_main would be
        # a coroutine, not an AsyncMock, and assert_awaited would fail.
        stub.assemble_main.assert_awaited()  # type: ignore[attr-defined]


# ─── Process: builder outputs ───────────────────────────────────────────────


class TestBuilderOutputs:
    """Verify builder.pool, builder.descriptor, builder.strategy_result."""

    async def test_builder_pool_is_agent_pool_instance(self) -> None:
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        # builder.pool is the SUPPLIED pool (identity-preserved).
        assert builder.pool is builder.infra.pool

    async def test_builder_descriptor_is_agent_descriptor_instance(self) -> None:
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        descriptor = getattr(builder, "descriptor", None)
        assert descriptor is not None
        assert isinstance(descriptor, AgentDescriptor)
        assert descriptor.address.name == "test_agent"

        await builder.cleanup()

    async def test_builder_strategy_result_is_set(self) -> None:
        """``builder.strategy_result`` is the StrategyAssembly from assemble."""
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        assert builder.strategy_result is not None
        # The strategy_result is the mock StrategyAssembly returned by
        # the stub's assemble method.
        assert builder.strategy_result is stub._mock_assembly  # type: ignore[attr-defined]

        await builder.cleanup()

    async def test_supplied_pool_not_registered_for_cleanup(self) -> None:
        """No cleanup registration — the orchestrator owns the supplied
        pool's lifecycle (create_pool tears it down via workspace evict)."""
        stub = _make_stub_strategy()
        registry = _make_registry(stub)
        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        assert builder._cleanups == []  # noqa: SLF001


# ─── Process: ctx.pool_runtime propagation ──────────────────────────────────


class TestPoolRuntimePropagation:
    """Verify PoolRuntimeDeps is built and propagated via dataclasses.replace."""

    async def test_pool_runtime_deps_built_with_pool_assembly_ctx(self) -> None:
        """The factory.create receives an updated ctx with pool_runtime set."""
        stub = _make_stub_strategy()
        registry = _make_registry(stub)

        # Wrap factory.create to capture the ctx passed to it.
        factory = registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "stub")
        original_create = factory.create
        captured_ctx: list[AssemblyContext] = []

        async def _capturing_create(
            config: Any, ctx: AssemblyContext
        ) -> _StubExecutionStrategy:
            captured_ctx.append(ctx)
            return await original_create(config, ctx)

        factory.create = _capturing_create  # type: ignore[method-assign]

        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = _make_supply()
        ctx = _make_ctx(registry, builder.workspace_resources, builder.infra)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        assert len(captured_ctx) == 1
        passed_ctx = captured_ctx[0]
        assert passed_ctx.pool_runtime is not None
        assert isinstance(passed_ctx.pool_runtime, PoolRuntimeDeps)
        assert passed_ctx.pool_runtime.pool_assembly_ctx is not None
        assert isinstance(
            passed_ctx.pool_runtime.pool_assembly_ctx, PoolAssemblyContext
        )

        await builder.cleanup()


# ─── Supply-mode production shape (factory.py:349-352) ──────────────────────


class TestSupplyModeRequired:
    """Supply is REQUIRED (SPEC Errata-5) — missing supply raises."""

    async def test_missing_supply_raises(self) -> None:
        stub_strategy = _StubExecutionStrategy()
        registry = _make_registry(stub_strategy)

        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        ctx = _make_ctx(registry, builder.workspace_resources, None)
        spec = _make_spec()
        stage = PoolAssembleStage()

        with pytest.raises(ValueError, match="supply-mode"):
            await stage.process(spec, builder, ctx)

    async def test_supply_used_verbatim(self):
        """The supplied ctx and pool flow through unchanged."""
        stub_strategy = _make_stub_strategy()
        registry = _make_registry(stub_strategy)

        pre_built_ctx = _make_pool_assembly_ctx()
        pre_built_pool = MagicMock(spec=AgentPool)
        supply = SupplyInfra(
            pool_assembly_ctx=pre_built_ctx,
            pool=pre_built_pool,
        )

        builder = AssemblyBuilder()
        builder.workspace_resources = _make_workspace_resources()
        builder.infra = supply
        ctx = _make_ctx(registry, builder.workspace_resources, supply)
        spec = _make_spec()
        stage = PoolAssembleStage()

        await stage.process(spec, builder, ctx)

        assert builder.pool is pre_built_pool
        assert builder.strategy_result is not None
        passed = stub_strategy.assemble_main.call_args.args[0]  # type: ignore[attr-defined]
        assert passed is pre_built_ctx

    async def test_supply_mode_pool_runtime_session_tree_optional(self):
        """PoolRuntimeDeps.session_tree_manager must accept None.

        The tree_manager is built after the pipeline returns
        (factory.py:498), so it cannot be in the infra dict during
        pipeline.run(). The field must be optional.
        """
        deps = PoolRuntimeDeps(
            session_tree_manager=None,  # type: ignore[arg-type]
            pool_assembly_ctx=_make_pool_assembly_ctx(),
        )
        assert deps.session_tree_manager is None
        assert deps.pool_assembly_ctx is not None

# ─── Pool-level extension type guards (ticket 10) ────────────────────────────


class _WrongProductFactory(SimpleFactory):
    """Produces a plain object — the WRONG kind for a guarded slot."""

    def __init__(self) -> None:
        super().__init__(object(), _StubStrategyConfig)


class TestExtensionTypeGuards:
    """A roster-named INTERCEPTOR / COMMAND_HANDLER factory whose product
    is the wrong kind must fail loudly (TypeError) at Stage 3 — never
    silently wire a bogus object into the pipeline. (Migrated from the BIZ
    ``_resolve_pipeline_extensions`` tests when the resolution moved into
    the stage, ticket 10.)"""

    def _spec_with(self, **kwargs: object) -> AssemblySpec:
        return _make_spec().model_copy(update=kwargs)

    async def test_interceptor_factory_wrong_product_type_raises(self) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.INTERCEPTOR, "probe_bad", _WrongProductFactory()
        )
        ctx = AssemblyContext(
            registry=registry, workspace_ctx=_make_workspace_ctx()
        )
        stage = PoolAssembleStage()

        with pytest.raises(
            TypeError, match="INTERCEPTOR component 'probe_bad' did not create Interceptor"
        ):
            await stage._resolve_interceptor_chain(
                self._spec_with(interceptors=["probe_bad"]), ctx
            )

    async def test_command_factory_wrong_product_type_raises(self) -> None:
        registry = ComponentRegistry()
        registry.register(
            ComponentSlot.COMMAND_HANDLER, "probe_bad_cmd", _WrongProductFactory()
        )
        ctx = AssemblyContext(
            registry=registry, workspace_ctx=_make_workspace_ctx()
        )
        stage = PoolAssembleStage()

        with pytest.raises(
            TypeError,
            match="COMMAND_HANDLER component 'probe_bad_cmd' did not create CommandHandler",
        ):
            await stage._resolve_command_processor(
                self._spec_with(commands=["probe_bad_cmd"]), ctx
            )

    async def test_no_roster_additions_resolve_to_none(self) -> None:
        """Empty interceptor roster / absent commands → ``None`` products —
        the orchestrator keeps the shared chain / passed-in processor."""
        ctx = AssemblyContext(
            registry=ComponentRegistry(), workspace_ctx=_make_workspace_ctx()
        )
        stage = PoolAssembleStage()

        assert await stage._resolve_interceptor_chain(_make_spec(), ctx) is None
        assert await stage._resolve_command_processor(_make_spec(), ctx) is None
