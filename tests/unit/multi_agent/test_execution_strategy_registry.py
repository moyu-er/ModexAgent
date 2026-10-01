"""Unit tests for ExecutionStrategyRegistry + frozen dataclasses (ADR-0025 D1/D2, Ticket 1)."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    ExecutionStrategyRegistry,
    MainAssembly,
    PoolAssemblyContext,
    StrategyAssembly,
    StrategyComponentFactory,
    default_strategy_registry,
    strategy_registry_from_components,
)
from modex_agent.scope.component_registry import (
    ComponentRegistry,
)
from modex_agent.scope.components import ComponentSlot, SimpleFactory
from modex_agent.scope.runtime_ownership import RuntimeOwnership, StrategyManifest
from modex_agent.scope.spec import AgentSpec, PoolSpec


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _StubStrategy(ExecutionStrategy):
    """Minimal concrete strategy for registry mechanics tests."""

    @property
    def name(self) -> str:
        return "stub"

    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        raise NotImplementedError


def test_default_strategy_registry_is_empty() -> None:
    """default_strategy_registry() returns an empty registry."""
    reg = default_strategy_registry()
    assert reg.names() == []


def test_register_then_resolve_returns_same_instance() -> None:
    """register(strategy) then resolve(name) returns the same instance."""
    reg = ExecutionStrategyRegistry()
    strategy = _StubStrategy()
    reg.register(strategy)
    assert reg.resolve("stub") is strategy


def test_component_registry_derives_same_strategy_instance() -> None:
    component_registry = ComponentRegistry()
    strategy = _StubStrategy()
    component_registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        strategy.name,
        StrategyComponentFactory(strategy),
    )

    registry = strategy_registry_from_components(component_registry)

    assert registry.resolve(strategy.name) is strategy


def test_strategy_factory_probes_the_ownership_manifest() -> None:
    """The slot's probe face: ``probe()`` surfaces the StrategyManifest —
    the synchronous ownership contract the scope compiler reads."""
    strategy = _StubStrategy()
    factory = StrategyComponentFactory(strategy)

    manifest = factory.probe()

    assert manifest == StrategyManifest(name="stub", ownership=RuntimeOwnership())
    assert (
        asyncio.run(
            factory.create(factory.config_model(), None)  # type: ignore[arg-type]
        )
        is strategy
    )


def test_component_registry_skips_non_strategy_component_factory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    component_registry = ComponentRegistry()
    component_registry.register(
        ComponentSlot.EXECUTION_STRATEGY,
        "invalid",
        SimpleFactory("not-a-strategy", _EmptyConfig),
    )

    registry = strategy_registry_from_components(component_registry)

    assert registry.names() == []
    assert "expected StrategyComponentFactory" in caplog.text


def test_register_duplicate_name_raises_value_error() -> None:
    """register with a duplicate name raises ValueError."""
    reg = ExecutionStrategyRegistry()
    reg.register(_StubStrategy())
    with pytest.raises(ValueError, match="Duplicate execution strategy: stub"):
        reg.register(_StubStrategy())


def test_resolve_unknown_name_raises_value_error() -> None:
    """resolve with an unknown name raises ValueError listing registered names."""
    reg = ExecutionStrategyRegistry()
    with pytest.raises(ValueError, match="Unknown execution strategy: 'nope'"):
        reg.resolve("nope")


def test_names_returns_sorted_list() -> None:
    """names() returns registered strategy names sorted alphabetically."""

    class _BetaStrategy(_StubStrategy):
        @property
        def name(self) -> str:
            return "beta"

    class _AlphaStrategy(_StubStrategy):
        @property
        def name(self) -> str:
            return "alpha"

    reg = ExecutionStrategyRegistry()
    reg.register(_BetaStrategy())
    reg.register(_AlphaStrategy())
    assert reg.names() == ["alpha", "beta"]


def test_default_ownership_is_the_react_shape() -> None:
    """The ABC's default ownership mirrors the bundled react shape."""
    strategy = _StubStrategy()
    assert strategy.ownership == RuntimeOwnership(
        needs_llm_provider=True,
        needs_main_agent_tools=True,
        needs_memory=True,
        supports_approval=True,
        supports_subagents=True,
        owns_context=False,
    )


def test_ownership_is_frozen_and_closed() -> None:
    """RuntimeOwnership is a frozen, extra-forbidden value object."""
    ownership = RuntimeOwnership()
    with pytest.raises(ValidationError):
        ownership.supports_subagents = False
    with pytest.raises(ValidationError):
        RuntimeOwnership(bogus_flag=True)


def _make_minimal_context() -> PoolAssemblyContext:
    """Build a PoolAssemblyContext with MagicMock stand-ins for ABC fields.

    Only the 11 required (no-default) fields are passed; the 20 optional
    fields receive their defaults (``None`` or ``[]`` for ``shared_hooks``).
    """
    return PoolAssemblyContext(
        pool_name="test-pool",
        pool_spec=MagicMock(),
        project_dir=Path("/tmp/project"),
        data_dir=Path("/tmp/data"),
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=MagicMock(),
        retention=MagicMock(),
        registry=MagicMock(),
    )


def test_pool_assembly_context_is_frozen() -> None:
    """PoolAssemblyContext rejects attribute mutation (frozen dataclass)."""
    ctx = _make_minimal_context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.pool_name = "mutated"  # type: ignore[misc]


def test_pool_assembly_context_optional_defaults() -> None:
    """PoolAssemblyContext optional fields default to None / []."""
    ctx = _make_minimal_context()
    assert ctx.workspace_handle is None
    assert ctx.workspace_resolver is None
    assert ctx.emitter_factory is None
    assert ctx.app_config is None
    assert ctx.persistence is None
    assert ctx.mcp_registry is None
    assert ctx.shared_hooks == []
    assert ctx.shared_hook_runner is None
    assert ctx.shared_interceptor_chain is None
    assert ctx.session_registry is None
    assert ctx.session_store is None
    assert ctx.bot_model_config is None
    assert ctx.model_choice_registry is None
    assert ctx.command_processor is None
    assert ctx.control_channel is None
    assert ctx.pool_data is None
    assert ctx.record_scope is None
    assert ctx.on_session_start is None
    assert ctx.on_session_end is None
    assert ctx.router is None


def test_strategy_assembly_is_frozen() -> None:
    """StrategyAssembly rejects attribute mutation (frozen dataclass)."""
    assembly = StrategyAssembly(
        tool_manager=MagicMock(),
        context_manager=MagicMock(),
        notification_service=MagicMock(),
        target_store=MagicMock(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        assembly.tool_manager = MagicMock()  # type: ignore[misc]


def test_strategy_assembly_optional_defaults() -> None:
    """StrategyAssembly optional fields default to None — the shared
    contract only; per-shape products ride their typed payloads."""
    assembly = StrategyAssembly(
        tool_manager=MagicMock(),
        context_manager=MagicMock(),
        notification_service=MagicMock(),
        target_store=MagicMock(),
    )
    assert assembly.control_channel is None
    assert assembly.root_provider is None
    # Per-shape products
    assert assembly.react_products is None
    assert assembly.runtime_constructor is None
    assert assembly.main is None
    # The retired transitional/dict fields are gone (W5)
    field_names = {field.name for field in dataclasses.fields(assembly)}
    assert "external_deps" not in field_names
    assert "agent" not in field_names
    assert "turn_runner" not in field_names
    assert "extra_cleanup" not in field_names


def test_validate_pool_spec_derives_subagent_rule_from_ownership() -> None:
    """The base validate_pool_spec rejects subagents when ownership
    declares supports_subagents=False (byte-identical message shape)."""

    class _SoloStrategy(_StubStrategy):
        @property
        def ownership(self) -> RuntimeOwnership:
            return RuntimeOwnership(supports_subagents=False)

    pool = PoolSpec(
        name="p",
        agents=[AgentSpec(name="root"), AgentSpec(name="sub", parent="root")],
    )
    _StubStrategy().validate_pool_spec(pool)  # default ownership accepts
    with pytest.raises(ValueError, match="does not support subagents"):
        _SoloStrategy().validate_pool_spec(pool)


def test_main_assembly_carries_descriptor_and_instance() -> None:
    descriptor = MagicMock()
    instance = MagicMock()
    main = MainAssembly(descriptor=descriptor, instance=instance)
    assembly = StrategyAssembly(main=main)
    assert assembly.main is main
    assert assembly.main.descriptor is descriptor
    assert assembly.main.instance is instance
