"""Runtime ownership — the execution-strategy contract vocabulary (W5).

``scope`` owns the DECLARATION vocabulary: what a pool shape's runtime
OWNS is declared as one frozen :class:`RuntimeOwnership` model, surfaced
synchronously through the EXECUTION_STRATEGY slot's factory probe face
(:class:`StrategyManifest`). ``multi_agent`` (which sits above ``scope``
in the layering tree) imports these types downward for its
:class:`~modex_agent.multi_agent.execution_strategy.ExecutionStrategy`
ABC — the compiler and the tree validator derive structural rules from
them WITHOUT importing any strategy implementation.

Every axis is mechanically consumed (compile-time unless noted):

- ``needs_llm_provider`` — ``create_pool`` resolves the LLM_PROVIDER slot
  only for strategies declaring ``True``; the native core skips its
  provider fallback otherwise (assembly time).
- ``needs_main_agent_tools`` — ``create_pool`` registers the
  communication tool surface and runs ``wire_main_pipeline`` only for
  ``True`` (assembly time).
- ``needs_memory`` — ``False``: a memory-surface request (``memory:``
  overrides or ``memory_system:``) is a compile-time error, and the
  native assembly threads no memory products.
- ``supports_approval`` — ``False``: the schema still ACCEPTS a root
  approval declaration, but the bill reports approval NOT applicable
  (enabled/eligible false) so the friendly form never claims support the
  strategy cannot deliver; native assembly threads no approval surface.
- ``supports_subagents`` — the strategy ABC's base ``validate_pool_spec``
  rejects subagent templates on ``False`` (assembly time).
- ``owns_context`` — ``True``: the compiler drops the position-default
  hook face and rejects capability declarations (V12), and the native
  assembly builds no framework context manager.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.scope.component_registry import (
    ComponentNotFoundError,
    ComponentRegistry,
)
from modex_agent.scope.components import ComponentSlot

__all__ = [
    "BUNDLED_EXTERNAL_OWNERSHIP",
    "BUNDLED_REACT_OWNERSHIP",
    "RuntimeOwnership",
    "StrategyManifest",
    "bundled_strategy_ownership",
    "resolve_strategy_ownership",
]


class RuntimeOwnership(BaseModel):
    """Positive declaration of what an execution strategy's runtime OWNS.

    The single frozen contract replacing the former ad-hoc capability
    flags (``supports_subagents`` / ``requires_main_agent_tools`` /
    ``requires_llm_provider``). Defaults describe the bundled react shape
    (the framework owns every component face); self-owning shapes like
    ``external`` declare the opposite on each axis they own. See the
    module docstring for the mechanical consumer of every axis.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    needs_llm_provider: bool = True
    needs_main_agent_tools: bool = True
    needs_memory: bool = True
    supports_approval: bool = True
    supports_subagents: bool = True
    owns_context: bool = False


class StrategyManifest(BaseModel):
    """A strategy's synchronous introspection payload (the probe face).

    What an EXECUTION_STRATEGY slot factory's ``probe()`` must surface:
    the registered name plus the strategy's :class:`RuntimeOwnership`.
    The scope compiler and the tree validator read ownership through
    this manifest — synchronous, no ``await``, no import of
    ``multi_agent``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ownership: RuntimeOwnership


BUNDLED_REACT_OWNERSHIP: Final[RuntimeOwnership] = RuntimeOwnership()
"""The bundled ``react`` shape — the framework owns every component face;
the memory system builds the context."""

BUNDLED_EXTERNAL_OWNERSHIP: Final[RuntimeOwnership] = RuntimeOwnership(
    needs_llm_provider=False,
    needs_main_agent_tools=False,
    needs_memory=False,
    supports_approval=False,
    supports_subagents=False,
    owns_context=True,
)
"""The bundled ``external`` shape — the CLI harness owns model config,
tools, memory, context, and approval; no subagent templates ride the
pool."""

_BUNDLED_OWNERSHIP: Final[dict[str, RuntimeOwnership]] = {
    ExecutionStrategyKind.REACT.value: BUNDLED_REACT_OWNERSHIP,
    ExecutionStrategyKind.EXTERNAL.value: BUNDLED_EXTERNAL_OWNERSHIP,
}


def bundled_strategy_ownership(name: str) -> RuntimeOwnership | None:
    """The ownership of a framework-bundled strategy name, or ``None``.

    These two constants are the single owner of the bundled shapes'
    ownership: the bundled strategies' ``ownership`` properties return
    them, so the compile-time fallback and the runtime declaration can
    never drift apart.
    """
    return _BUNDLED_OWNERSHIP.get(name)


def resolve_strategy_ownership(
    name: str,
    registry: ComponentRegistry | None,
) -> RuntimeOwnership:
    """Resolve a declared execution-strategy name to its ownership contract.

    ONE mechanism for the tree validator (V12), the compiler (hook face,
    capability defense, memory/approval rules), and any future
    compile-time consumer. Resolution order:

    1. The registry's EXECUTION_STRATEGY slot — the factory's
       ``probe()`` must surface a :class:`StrategyManifest` (the slot's
       probe contract; register strategies through
       ``StrategyComponentFactory``). A plugin-registered name —
       including an override of a bundled name — resolves here, so an
       unregistered plugin name is a boot error one compile cycle before
       the assembly-time slot resolution (the V13 precedent).
    2. The framework-bundled enum shapes (``react`` / ``external``) —
       the declaration vocabulary's own names, resolvable without a
       registry (``compile_scope(registry=None)`` stays usable for
       bundled-only trees; registries that never loaded the bundled
       strategies keep compiling).

    Raises:
        ComponentNotFoundError: a plugin name absent from the slot.
        ValueError: ``registry is None`` and the name is not bundled, or
            the registered factory's probe did not surface a manifest.
    """
    if registry is not None:
        factory = registry.factories(ComponentSlot.EXECUTION_STRATEGY).get(name)
        if factory is not None:
            # Extension boundary: third-party factories own their probe
            # product; the slot contract pins it to StrategyManifest.
            probed = factory.probe()
            if not isinstance(probed, StrategyManifest):
                raise ValueError(
                    f"execution strategy {name!r}: factory probe() must "
                    f"surface a StrategyManifest, got {type(probed).__name__} "
                    "— register strategies through StrategyComponentFactory"
                )
            return probed.ownership
    bundled = bundled_strategy_ownership(name)
    if bundled is not None:
        return bundled
    if registry is None:
        raise ValueError(
            f"registry required when execution_strategy {name!r} is "
            "referenced: pass the ComponentRegistry used at boot "
            "(registry=None supports only the framework-bundled "
            "strategies react/external)"
        )
    raise ComponentNotFoundError(name, ComponentSlot.EXECUTION_STRATEGY)
