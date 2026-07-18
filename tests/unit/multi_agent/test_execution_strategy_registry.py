"""Unit tests for ExecutionStrategyRegistry + frozen dataclasses (ADR-0025 D1/D2, Ticket 1)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.multi_agent.execution_strategy import (
    ExecutionStrategy,
    ExecutionStrategyRegistry,
    PoolAssemblyContext,
    StrategyAssembly,
    default_strategy_registry,
)
from modex_agent.multi_agent.pool_config.specs import PoolSpec


class _StubStrategy(ExecutionStrategy):
    """Minimal concrete strategy for registry mechanics tests."""

    @property
    def name(self) -> str:
        return "stub"

    async def assemble(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        raise NotImplementedError

    def validate_pool_spec(self, spec: PoolSpec) -> None:
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


def test_default_capability_flags() -> None:
    """supports_subagents and requires_main_agent_tools default to True."""
    strategy = _StubStrategy()
    assert strategy.supports_subagents is True
    assert strategy.requires_main_agent_tools is True


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
    assert ctx.transcript_store is None
    assert ctx.on_session_start is None
    assert ctx.on_session_end is None
    assert ctx.router is None


def test_strategy_assembly_is_frozen() -> None:
    """StrategyAssembly rejects attribute mutation (frozen dataclass)."""
    assembly = StrategyAssembly(
        agent=MagicMock(),
        turn_runner=MagicMock(),
        notification_service=MagicMock(),
        communication_service=MagicMock(),
        target_store=MagicMock(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        assembly.agent = MagicMock()  # type: ignore[misc]


def test_strategy_assembly_optional_defaults() -> None:
    """StrategyAssembly optional fields default to None and extra_cleanup to ()."""
    assembly = StrategyAssembly(
        agent=MagicMock(),
        turn_runner=MagicMock(),
        notification_service=MagicMock(),
        communication_service=MagicMock(),
        target_store=MagicMock(),
    )
    # React-only (None for external_coding)
    assert assembly.provider is None
    assert assembly.tool_manager is None
    assert assembly.skill_manager is None
    assert assembly.mcp_manager is None
    assert assembly.terminal_manager is None
    assert assembly.context_manager is None
    assert assembly.dream_engine is None
    assert assembly.dream_interval is None
    assert assembly.command_processor is None
    assert assembly.control_channel is None
    # External-only (None for react)
    assert assembly.backend is None
    assert assembly.session_map_store is None
    # Cleanup hooks
    assert assembly.extra_cleanup == ()
