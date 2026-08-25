"""Register shipped strategies and the pool-runtime bot LLM factory.

The plugin owns the business-side ``react`` and ``external`` strategy
registrations plus ``bot_default``, whose construction depends on per-pool
runtime objects unavailable to framework defaults.

The strategies are stateless (``assemble()`` is called once per pool at
build time), so a single instance per strategy is created here and reused
by every factory ``create()`` call. The ``bash`` tool factory lives in FW
defaults (``plugins/defaults/tools.py``) — it reads the terminal manager
from ``PoolRuntimeDeps``, which the BIZ populates per pool.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from bot.service.external_strategy import ExternalExecutionStrategy
from bot.service.model_provider import BotModelProvider
from bot.service.react_strategy import ReactExecutionStrategy
from pydantic import BaseModel

from modex_agent.core.provider import LLMProvider
from modex_agent.plugins.abc import ComponentFactory, SimpleFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext

if TYPE_CHECKING:
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext


class BotStrategiesConfig(BaseModel):
    """Empty config schema — the shipped strategies need no configuration."""

    model_config = {"frozen": True, "extra": "forbid"}


class BotDefaultLLMConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


def _pool_assembly(
    ctx: AssemblyContext, component_name: str
) -> PoolAssemblyContext:
    pool_runtime = ctx.pool_runtime
    pool_assembly = (
        pool_runtime.pool_assembly_ctx if pool_runtime is not None else None
    )
    if pool_assembly is None:
        raise ValueError(
            f"pool_assembly_ctx is required for {component_name}; "
            "reference it from a pool roster"
        )
    return pool_assembly


class BotDefaultLLMFactory(ComponentFactory):
    config_model = BotDefaultLLMConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> LLMProvider:
        del config
        pool_assembly = _pool_assembly(ctx, "bot_default")
        if pool_assembly.bot_model_config is None:
            raise ValueError(
                "bot_model_config is required for bot_default; configure the "
                "model dependency in the pool roster"
            )
        if pool_assembly.model_choice_registry is None:
            raise ValueError(
                "model_choice_registry is required for bot_default; configure "
                "the model dependency in the pool roster"
            )
        return BotModelProvider(pool_assembly.bot_model_config)


_REACT_STRATEGY = ReactExecutionStrategy()
_EXTERNAL_STRATEGY = ExternalExecutionStrategy()


class BotStrategiesPlugin(Plugin):
    """Registers the ``react`` + ``external`` execution strategies."""

    config_model = BotStrategiesConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_execution_strategy(
            "react", SimpleFactory(_REACT_STRATEGY, BotStrategiesConfig)
        )
        ctx.register_execution_strategy(
            "external", SimpleFactory(_EXTERNAL_STRATEGY, BotStrategiesConfig)
        )
        ctx.register_provider("bot_default", BotDefaultLLMFactory())
