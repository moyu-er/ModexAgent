"""Tests for BotStrategiesPlugin — registers react + external strategies.

Verifies the business-side plugin (``examples/bot_project/plugins/``)
registers both shipped execution strategies into the
``EXECUTION_STRATEGY`` slot via ``SimpleFactory`` wrappers, and that the
factories return the stateless strategy instances (created once,
reused).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Ensure bot_project is importable (bot.* + plugins.*)
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from bot.service.external_strategy import ExternalExecutionStrategy  # noqa: E402
from bot.service.react_strategy import ReactExecutionStrategy  # noqa: E402
from plugins.bot_strategies import BotStrategiesConfig, BotStrategiesPlugin  # noqa: E402

from modex_agent.plugins.abc import ComponentSlot  # noqa: E402
from modex_agent.plugins.loader import PluginRegistrationContext  # noqa: E402
from modex_agent.plugins.registry import ComponentRegistry  # noqa: E402


def _register_plugin() -> ComponentRegistry:
    registry = ComponentRegistry()
    plugin = BotStrategiesPlugin()
    with PluginRegistrationContext(registry) as ctx:
        plugin.register(ctx)
    return registry


class TestBotStrategiesPlugin:
    def test_config_model_is_frozen_empty_schema(self) -> None:
        config = BotStrategiesConfig()
        with pytest.raises(ValidationError):
            config.anything = 1  # type: ignore[attr-defined]

    def test_registers_both_strategies_into_execution_strategy_slot(self) -> None:
        registry = _register_plugin()
        assert registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "react")
        assert registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "external")

    def test_slot_contains_exactly_react_and_external(self) -> None:
        registry = _register_plugin()
        slot_names = set(
            registry._factories.get(ComponentSlot.EXECUTION_STRATEGY, {})  # noqa: SLF001
        )
        assert slot_names == {"react", "external"}

    async def test_react_factory_returns_react_strategy_instance(self) -> None:
        registry = _register_plugin()
        factory = registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "react")
        instance = await factory.create(BotStrategiesConfig(), None)  # type: ignore[arg-type]
        assert isinstance(instance, ReactExecutionStrategy)

    async def test_external_factory_returns_external_strategy_instance(self) -> None:
        registry = _register_plugin()
        factory = registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "external")
        instance = await factory.create(BotStrategiesConfig(), None)  # type: ignore[arg-type]
        assert isinstance(instance, ExternalExecutionStrategy)

    async def test_strategies_are_stateless_singletons(self) -> None:
        """SimpleFactory returns the same instance on every create()."""
        registry = _register_plugin()
        factory = registry.resolve(ComponentSlot.EXECUTION_STRATEGY, "react")
        first = await factory.create(BotStrategiesConfig(), None)  # type: ignore[arg-type]
        second = await factory.create(BotStrategiesConfig(), None)  # type: ignore[arg-type]
        assert first is second
