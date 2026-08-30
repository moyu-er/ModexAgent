"""TDD tests for WorkspaceMaterializeStage + InfraAssembleStage.

Verifies the two AssemblyStage subclasses (tasks 17-18 of the
scope-converge implementation plan):

- :class:`WorkspaceMaterializeStage`:
  - Supplied-mode no-op: ``ctx.workspace_resources`` pre-set → stage
    skips entirely (zero side effects — registry.materialize NOT called,
    builder.workspace_resources NOT mutated, no cleanup registered).
    CRITICAL: this prevents recursive single-flight deadlock when the
    stage runs inside a ResourceFactory.materialize body that already
    contains a pool loop calling create_pool
    (resources.py:83-107,261-302).
  - Normal mode: calls ``workspace_registry.materialize(ctx.workspace_ctx)``
    and sets ``builder.workspace_resources`` to the returned value.
- :class:`InfraAssembleStage`:
  - Supplied-mode no-op: ``builder.infra`` pre-set → stage skips.
  - Missing supply raises (supply-mode contract, SPEC Errata-5).
  - Copies the orchestrator's supply to ``builder.infra`` verbatim.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.plugins.abc import AgentType
from modex_agent.plugins.assembly.builder import AssemblyBuilder
from modex_agent.plugins.assembly.context import AssemblyContext, SupplyInfra
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.assembly.stages.infra_assemble import InfraAssembleStage
from modex_agent.plugins.assembly.stages.workspace_materialize import (
    WorkspaceMaterializeStage,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_stages_ws")
    return WorkspaceContext(
        target=target,
        paths=WorkspacePaths(root=target),
        is_home=False,
    )


def _make_spec(agent_type: AgentType = AgentType.native_main) -> AssemblySpec:
    """Minimal AssemblySpec — stages read agent_type + workspace_ctx only."""
    return AssemblySpec(
        agent_type=agent_type,
        agent_name="test_agent",
        pool_name="test_pool",
        tools=[],
        hooks=[],
        llm_provider="test",
        system_prompt_provider="test",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="native",
        workspace_ctx=_make_workspace_ctx(),
    )


def _make_ctx(
    *,
    registry: ComponentRegistry | None = None,
    workspace_resources: Any | None = None,
) -> AssemblyContext:
    """Build a minimal AssemblyContext for stage tests.

    ``workspace_registry`` is a MagicMock — WorkspaceMaterializeStage
    only calls ``.materialize(ctx.workspace_ctx)`` on it, so an AsyncMock
    on that single method suffices. Real ScopeRegistry construction
    requires a ResourceFactory + store + home path — unnecessary overhead
    for testing stage orchestration.
    """
    ws_registry = MagicMock()
    ws_registry.materialize = AsyncMock(return_value=workspace_resources)
    return AssemblyContext(
        registry=registry if registry is not None else ComponentRegistry(),
        workspace_registry=ws_registry,
        workspace_ctx=_make_workspace_ctx(),
        workspace_resources=workspace_resources,
    )


# ─── WorkspaceMaterializeStage: supplied-mode no-op ─────────────────────────


class TestWorkspaceMaterializeSuppliedNoOp:
    """CRITICAL: pre-set ``ctx.workspace_resources`` → stage skips registry call.

    This prevents recursive single-flight deadlock when the stage runs
    inside a ResourceFactory.materialize body that already contains a
    pool loop calling create_pool (resources.py:83-107, 261-302).
    Without the skip, the chain
    factory.materialize → create_pool → pipeline.run →
    WorkspaceMaterializeStage → registry.materialize → _materialize_once
    would re-enter the in-flight task for the same target key and
    deadlock (awaiting the very task currently running).

    The stage propagates ctx.workspace_resources to builder.workspace_resources
    so downstream stages (PoolAssembleStage) can access it without a separate
    copy step. The registry.materialize call is still skipped.
    """

    async def test_supplied_mode_skips_materialize_call(self) -> None:
        """ctx.workspace_resources pre-set → registry.materialize NOT called."""
        sentinel = object()
        ctx = _make_ctx(workspace_resources=sentinel)
        builder = AssemblyBuilder()
        stage = WorkspaceMaterializeStage()

        await stage.process(_make_spec(), builder, ctx)

        # materialize NOT called — zero side effects on the registry.
        # type: ignore below: workspace_registry is a MagicMock in tests,
        # but typed ScopeRegistry[Any] in the dataclass — the type
        # checker cannot see the Mock substitution.
        ctx.workspace_registry.materialize.assert_not_called()  # type: ignore[attr-defined]

    async def test_supplied_mode_propagates_to_builder(self) -> None:
        """builder.workspace_resources set to ctx.workspace_resources.

        The stage copies the supplied value to builder.workspace_resources
        so downstream stages (PoolAssembleStage) can read it. The stage's
        contract is "materialize if absent, propagate if supplied".
        """
        sentinel = object()
        ctx = _make_ctx(workspace_resources=sentinel)
        builder = AssemblyBuilder()
        stage = WorkspaceMaterializeStage()

        await stage.process(_make_spec(), builder, ctx)

        assert builder.workspace_resources is sentinel

    async def test_supplied_mode_registers_no_cleanup(self) -> None:
        """Supplied-mode skip → no cleanup callback registered."""
        sentinel = object()
        ctx = _make_ctx(workspace_resources=sentinel)
        builder = AssemblyBuilder()
        stage = WorkspaceMaterializeStage()

        await stage.process(_make_spec(), builder, ctx)

        assert builder._cleanups == []  # noqa: SLF001


# ─── WorkspaceMaterializeStage: normal mode ─────────────────────────────────


class TestWorkspaceMaterializeNormalMode:
    """No pre-set workspace_resources → calls registry.materialize, sets builder."""

    async def test_normal_mode_calls_materialize_with_workspace_ctx(self) -> None:
        """registry.materialize called exactly once with ctx.workspace_ctx."""
        sentinel = object()
        ctx = _make_ctx()  # workspace_resources=None
        ctx.workspace_registry.materialize = AsyncMock(return_value=sentinel)
        builder = AssemblyBuilder()
        stage = WorkspaceMaterializeStage()

        await stage.process(_make_spec(), builder, ctx)

        ctx.workspace_registry.materialize.assert_awaited_once_with(  # type: ignore[attr-defined]
            ctx.workspace_ctx
        )

    async def test_normal_mode_sets_builder_workspace_resources(self) -> None:
        """builder.workspace_resources set to the materialize() return value."""
        sentinel = object()
        ctx = _make_ctx()
        ctx.workspace_registry.materialize = AsyncMock(return_value=sentinel)
        builder = AssemblyBuilder()
        stage = WorkspaceMaterializeStage()

        await stage.process(_make_spec(), builder, ctx)

        assert builder.workspace_resources is sentinel

    async def test_normal_mode_does_not_register_cleanup(self) -> None:
        """The stage does NOT register cleanup — ScopeRegistry owns eviction.

        The registry's ``evict_and_release`` / ``evict_all`` manage the
        R lifecycle; the assembly pipeline's cleanup contract concerns
        resources it created, not resources borrowed from the registry's
        cache.
        """
        ctx = _make_ctx()
        ctx.workspace_registry.materialize = AsyncMock(return_value=object())
        builder = AssemblyBuilder()
        stage = WorkspaceMaterializeStage()

        await stage.process(_make_spec(), builder, ctx)

        assert builder._cleanups == []  # noqa: SLF001


# ─── InfraAssembleStage: supply-mode contract ───────────────────────────────


class TestInfraAssembleSupplyContract:
    """Supply-mode only (SPEC Errata-5): the orchestrator pre-fills
    ``ctx.infra`` (SupplyInfra); the stage copies it to ``builder.infra``
    verbatim."""

    def _supply_ctx(self, supply: SupplyInfra | None) -> AssemblyContext:
        return AssemblyContext(
            registry=ComponentRegistry(),
            workspace_registry=MagicMock(),
            workspace_ctx=_make_workspace_ctx(),
            infra=supply,
        )

    async def test_missing_supply_raises(self) -> None:
        ctx = self._supply_ctx(None)
        builder = AssemblyBuilder()
        stage = InfraAssembleStage()

        with pytest.raises(ValueError, match="supply-mode"):
            await stage.process(_make_spec(), builder, ctx)

    async def test_supply_used_verbatim(self) -> None:
        """The supply lands on ``builder.infra`` by identity — the stage
        neither rebuilds nor augments it (the state_schema_compiler fill
        was deleted with the field; INC-4 convergence: the BIZ wiring is
        the single construction site)."""
        supply = SupplyInfra()
        ctx = self._supply_ctx(supply)
        builder = AssemblyBuilder()
        stage = InfraAssembleStage()

        await stage.process(_make_spec(), builder, ctx)

        assert builder.infra is not None
        assert builder.infra is supply
        assert builder.infra.pool_assembly_ctx is None
        assert builder.infra.pool is None

    async def test_supply_registers_no_cleanup(self) -> None:
        supply = SupplyInfra()
        ctx = self._supply_ctx(supply)
        builder = AssemblyBuilder()
        stage = InfraAssembleStage()

        await stage.process(_make_spec(), builder, ctx)

        assert builder._cleanups == []  # noqa: SLF001
