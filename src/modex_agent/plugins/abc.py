"""Plugin-unified agent assembly type hierarchy.

Defines the 11-slot component factory system's foundational types (SPEC
§4).

Design constraints:
- ABC-based interfaces, per rule 7 (no structural interfaces).
- Stages read ``ClassVar`` metadata (``hook_runner``, ``applies_to``)
  — never via ``isinstance`` (rule 9).
- ``config_model`` is a frozen Pydantic ``BaseModel`` (rule 12).
- ``create()`` is async because ``ExecutionStrategy.assemble`` is
  async (``execution_strategy.py``) and pipeline stages await
  ``factory.create(...)``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum, nonmember
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:
    # Forward reference: ``AssemblyContext`` and the context chain carriers
    # are defined in ``src/modex_agent/plugins/assembly/context.py``. Using
    # TYPE_CHECKING keeps type safety without a runtime import cycle.
    from modex_agent.plugins.assembly.context import AgentContext, AssemblyContext


class ComponentSlot(StrEnum):
    """The 11 component slots in the unified agent assembly system.

    Each slot names a distinct extension point that a plugin factory
    can produce. The slot set is authoritative (SPEC §4.3) — do not
    rename, reorder, or remove members. ``MEMORY_SYSTEM`` was added
    via SPEC Errata-7; ``CAPABILITY`` was added via ADR-0047; further
    additions require a new errata.
    """

    TOOL = "tool"
    HOOK = "hook"
    MEMORY_SYSTEM = "memory_system"
    LLM_PROVIDER = "llm_provider"
    SYSTEM_PROMPT_PROVIDER = "system_prompt_provider"
    INTERCEPTOR = "interceptor"
    COMMAND_HANDLER = "command_handler"
    EXECUTION_STRATEGY = "execution_strategy"
    INPUT_STAGE = "input_stage"
    DATA_NAMESPACE = "data_namespace"
    CAPABILITY = "capability"


class AgentType(StrEnum):
    """The four agent topology roles a component can target.

    Stages filter hooks by reading ``HookFactory.applies_to`` (a set
    of ``AgentType`` or ``None`` for "all types") — never via
    ``isinstance`` (rule 9).
    """

    native_main = "native_main"
    native_sub = "native_sub"
    external_main = "external_main"
    external_sub = "external_sub"


class HookRunnerKind(StrEnum):
    """The two hook runner backends a ``HookFactory`` can target.

    Stages dispatch hooks by reading ``HookFactory.hook_runner``
    ``ClassVar``, not via ``isinstance``. Exactly two values — adding
    a third is prohibited by design.
    """

    react = "react"
    memory = "memory"


class PluginSource(StrEnum):
    """The four plugin discovery sources (SPEC §4.1/§4.5).

    Attribution carrier for ``ComponentRegistry.register(source=...)`` and
    ``ComponentRegistry.registration_source()``: a same-source duplicate
    ``(slot, name)`` raises ``ValueError`` (a packaging/config error); a
    cross-source duplicate is resolved by :attr:`SOURCE_PRIORITY` rank —
    user > project > entry_points > bundled, nearest-to-user wins (SPEC
    §3.5 O2, ADR-0042). A ``None`` source (not a member) attributes a
    direct, loader-less registration, which preempts any source.
    """

    BUNDLED = "bundled"
    PROJECT = "project"
    USER = "user"
    ENTRY_POINTS = "entry_points"

    #: Explicit cross-source priority table (SPEC §3.5 O2): the higher
    #: rank wins a ``(slot, name)`` collision registered from two
    #: different sources. Read-only; consumers compare ranks and never
    #: assume discovery order decides the winner. ``nonmember`` keeps
    #: this class attribute out of the enum member set.
    SOURCE_PRIORITY = nonmember(
        MappingProxyType(
            {
                BUNDLED: 0,
                ENTRY_POINTS: 1,
                PROJECT: 2,
                USER: 3,
            }
        )
    )


class ComponentFactory(ABC):
    """Abstract factory for a single component slot.

    Subclasses declare:
    - ``config_model``: the frozen Pydantic ``BaseModel`` that
      validates the config for this component. Config is validated
      before ``create()`` is called.
    - ``create()``: async — produces the component instance. MUST be
      async because ``ExecutionStrategy.assemble`` is async
      (``execution_strategy.py``) and pipeline stages
      ``await factory.create(...)``.
    - ``applies_to`` / ``hook_runner`` / ``priority``: hook-dispatch
      metadata (``None`` / ``None`` / ``0`` on non-hook factories).
      Stages read these directly
      (rule 6 — no ``getattr``; rule 8 — declared on the base type).

    Return type is ``Any`` because ``ComponentFactory`` produces
    heterogeneous component types (Tool, Hook, etc.).
    A typed return would require a type-erased union that the
    slot-based dispatch does not benefit from. This is a justified
    escape from rule 3, documented here.

    The ``ctx`` parameter is the FULL-CHAIN :class:`AgentContext`
    (SPEC §3.3, ticket 04). A subclass WIDENS it to declare which layer
    it may read — the declared parameter type is the capability
    boundary (override variance):

    - ``ctx: PoolContext`` — pool-layer data only (todo store, terminal
      manager, communication facilities); workspace-layer fields are a
      type error.
    - ``ctx: WorkspaceContext`` — path layout + workspace resource
      handles (incl. the MCP shared handle).
    - ``ctx: AssemblyContext`` — the legacy pre-ticket view (legal;
      business factories keep this until their own migration tickets).
    - ``ctx: AgentContext`` — the full chain (all layers).

    Because ``AgentContext`` is a subtype of every declarable layer,
    the resolver passes one full-chain object to every factory and
    subtyping picks the readable surface — no runtime dispatch.
    """

    config_model: ClassVar[type[BaseModel]]
    applies_to: ClassVar[set[AgentType] | None] = None
    hook_runner: ClassVar[HookRunnerKind | None] = None
    priority: ClassVar[int] = 0
    """Hook-dispatch priority (0 on non-hook factories). Roster dispatch
    threads it into the ``HookSpec``; ``HookRunner`` sorts by it, so
    hooks that must run FIRST among their hook point declare negative
    values (e.g. tree-aware continuation hooks whose reminder should
    land before other hooks' reminders)."""

    @abstractmethod
    async def create(self, config: BaseModel, ctx: AgentContext) -> Any:
        """Produce the component instance.

        Args:
            config: already-validated config (an instance of
                ``config_model``). Callers run
                ``config_model.model_validate(...)`` before calling
                ``create()``; ``create()`` trusts the shape.
            ctx: the full-chain assembly context (forward ref — defined
                in ``assembly/context.py``). Subclasses widen this
                parameter to their required layer; see the class
                docstring for the capability-boundary contract.

        Returns:
            The component instance. Type depends on the slot.
        """
        ...


class SimpleFactory(ComponentFactory):
    """Factory wrapping a pre-built instance.

    ``create()`` ignores config and ctx, returns the wrapped instance.
    Useful for registering built-in components through the same
    factory pipeline as plugin-provided ones.

    ``config_model`` is set per-instance (each ``SimpleFactory``
    wraps a different component type) rather than at class definition
    time.
    """

    def __init__(
        self,
        instance: Any,
        config_model: type[BaseModel],
        applies_to: set[AgentType] | None = None,
        hook_runner: HookRunnerKind | None = None,
        priority: int = 0,
    ) -> None:
        self._instance = instance
        # config_model is declared as ClassVar on the parent;
        # SimpleFactory overrides it per-instance because it wraps
        # arbitrary components with different config schemas.
        self.config_model = config_model  # type: ignore[misc]
        # applies_to / hook_runner / priority: only set when wrapping hook
        # instances. None/0 means "not a hook factory" — stages read
        # the declared ClassVar directly (rule 6, rule 8).
        self.applies_to = applies_to  # type: ignore[misc]
        self.hook_runner = hook_runner  # type: ignore[misc]
        self.priority = priority  # type: ignore[misc]

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> Any:  # noqa: ARG002
        """Return the pre-built instance. Ignores config and ctx."""
        return self._instance


class HookFactory(ComponentFactory):
    """Abstract factory for hook components.

    Stages read ``hook_runner`` ``ClassVar`` to dispatch to the
    correct hook runner (react or memory) — never via ``isinstance``
    (rule 9). ``applies_to`` filters by ``AgentType``; ``None`` means
    "all types" (SPEC §6.7).

    ``applies_to`` and ``hook_runner`` are declared on
    :class:`ComponentFactory` as ``ClassVar[...] | None = None``.
    ``HookFactory`` re-declares them: ``applies_to`` with the same
    default (``None`` = all types), ``hook_runner`` without a default —
    subclasses MUST set it (use ``ReactHookFactory`` or
    ``MemoryHookFactory``). The type stays ``| None`` to satisfy the
    ClassVar invariant override rule; the stage's ``ValueError``
    fallback enforces the non-None contract at runtime. ``priority``
    (also from :class:`ComponentFactory`) is the factory-declared
    dispatch priority threaded into the ``HookSpec``.
    """

    applies_to: ClassVar[set[AgentType] | None] = None
    hook_runner: ClassVar[HookRunnerKind | None]


class ReactHookFactory(HookFactory):
    """Hook factory targeting the ReAct hook runner."""

    hook_runner: ClassVar[HookRunnerKind | None] = HookRunnerKind.react


class MemoryHookFactory(HookFactory):
    """Hook factory targeting the memory hook runner."""

    hook_runner: ClassVar[HookRunnerKind | None] = HookRunnerKind.memory
