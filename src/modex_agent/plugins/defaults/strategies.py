"""FW-bundled execution-strategy registration (W4a, plan SD-7).

Registers the two bundled pool-shape strategies — ``react`` (the ReAct
graph loop) and ``external`` (the OpenCode CLI harness) — into the
``EXECUTION_STRATEGY`` slot so a framework-only registry assembles a
runnable pool. Both are stateless singletons (``assemble_main`` runs once
per pool at build time), so one instance per strategy is shared by every
factory ``create()`` call.

``StrategyComponentFactory`` is the slot's registration face: its
``probe()`` surfaces the strategy's ``StrategyManifest``, making the
bundled shapes' ownership compile-visible (the same contract third-party
strategies register through).
"""

from __future__ import annotations

from modex_agent.multi_agent.execution_strategy import StrategyComponentFactory
from modex_agent.plugins.assembly.strategies import (
    ExternalExecutionStrategy,
    ReactExecutionStrategy,
)
from modex_agent.plugins.loader import PluginRegistrationContext

__all__ = [
    "register_default_strategies",
]


_REACT_STRATEGY = ReactExecutionStrategy()
_EXTERNAL_STRATEGY = ExternalExecutionStrategy()


def register_default_strategies(ctx: PluginRegistrationContext) -> None:
    """Register the bundled ``react`` + ``external`` execution strategies."""
    ctx.register_execution_strategy(
        "react", StrategyComponentFactory(_REACT_STRATEGY)
    )
    ctx.register_execution_strategy(
        "external", StrategyComponentFactory(_EXTERNAL_STRATEGY)
    )
