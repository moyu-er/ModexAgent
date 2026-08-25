"""Shared cost governance for Harbor production-pool evaluation."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Final

from plugins.bot_strategies import BotDefaultLLMConfig
from pydantic import BaseModel

from bot.eval.probes.budget import BudgetConfig, BudgetedProvider, BudgetLedger
from modex_agent.core.provider import LLMProvider
from modex_agent.plugins.abc import ComponentFactory, ComponentSlot
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.trace.pricing import PriceBook

POOL_BUDGET_ENV: Final = "MODEX_BUDGET_USD"
DEFAULT_POOL_BUDGET_USD: Final = 25.0
DEFAULT_CALL_RESERVE_USD: Final = 0.001
_BOT_DEFAULT_PROVIDER: Final = "bot_default"


class PoolBudgetEnvironmentError(ValueError):
    def __init__(self, raw_value: str) -> None:
        self.raw_value = raw_value
        super().__init__(f"{POOL_BUDGET_ENV} must be a positive finite number, got {raw_value!r}")


class _PoolBudgetFactory(ComponentFactory):
    config_model = BotDefaultLLMConfig

    def __init__(
        self,
        delegate: ComponentFactory,
        pricebook: PriceBook,
        budget_config: BudgetConfig,
    ) -> None:
        self._delegate = delegate
        self._pricebook = pricebook
        self._budget_config = budget_config
        self.ledger = BudgetLedger(budget_config.max_cost_usd)

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> LLMProvider:
        provider: LLMProvider = await self._delegate.create(config, ctx)
        return BudgetedProvider(
            provider,
            self._pricebook,
            self._budget_config,
            ledger=self.ledger,
        )


def pool_budget_config_from_env(
    environ: Mapping[str, str] | None = None,
) -> BudgetConfig:
    source = os.environ if environ is None else environ
    raw_value = source.get(POOL_BUDGET_ENV)
    if raw_value is None:
        max_cost_usd = DEFAULT_POOL_BUDGET_USD
    else:
        try:
            max_cost_usd = float(raw_value)
        except ValueError as error:
            raise PoolBudgetEnvironmentError(raw_value) from error
        if not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise PoolBudgetEnvironmentError(raw_value)
    return BudgetConfig(
        max_cost_usd=max_cost_usd,
        minimum_call_reserve_usd=DEFAULT_CALL_RESERVE_USD,
    )


def register_pool_budget(
    registry: ComponentRegistry,
    pricebook: PriceBook,
    budget_config: BudgetConfig,
) -> BudgetLedger:
    original = registry.resolve(ComponentSlot.LLM_PROVIDER, _BOT_DEFAULT_PROVIDER)
    factory = _PoolBudgetFactory(original, pricebook, budget_config)
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        _BOT_DEFAULT_PROVIDER,
        factory,
        overwrite=True,
    )
    return factory.ledger

