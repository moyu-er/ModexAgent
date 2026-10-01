"""Execution-strategy name coercion — the declaration-side vocabulary helper.

``strategy_name_of`` maps an :class:`~modex_agent.core.agent.ExecutionStrategyKind`
member or a plugin-registered component name onto the registry string the
compiler and assembly read. The enum itself stays in ``core.agent`` (hook
consumers sit at level 1 — only core is legal for all readers); this module
is the scope-side coercion home so the compiler no longer imports
``multi_agent`` (W1 layering surgery).
"""

from __future__ import annotations

from typing import assert_never

from modex_agent.core.agent import ExecutionStrategyKind

__all__ = ["strategy_name_of"]


def strategy_name_of(value: ExecutionStrategyKind | str) -> str:
    """Return the component-registry name for an execution strategy reference."""
    match value:
        case ExecutionStrategyKind():
            return value.value
        case str() as name:
            return name
        case unreachable:
            assert_never(unreachable)
