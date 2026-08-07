"""Default execution-strategy registry builder.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Builds a
registry with both shipped strategies registered, used when ``create_pool``
is called without an explicit ``strategy_registry``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
    )


def _default_strategy_registry() -> ExecutionStrategyRegistry:
    """Build a registry with both shipped strategies registered.

    Used when ``create_pool`` is called without an explicit
    ``strategy_registry`` (e.g. unit tests, legacy callers). Production wiring
    goes through ``BotService.initialize()`` which builds its own registry
    with the same strategies and threads it through ``wiring.py``.
    """
    from bot.service.external_strategy import (
        ExternalExecutionStrategy,
    )
    from bot.service.react_strategy import ReactExecutionStrategy
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
    )

    registry = ExecutionStrategyRegistry()
    registry.register(ReactExecutionStrategy())
    registry.register(ExternalExecutionStrategy())
    return registry
