"""ExecutionStrategy ABC + registry + assembly contract (ADR-0025).

A pool shape (ReAct graph loop, external CLI harness, future shapes) is
decided by an explicit :class:`ExecutionStrategy` ABC, not by scattered
``if execution_strategy ==`` branches in ``pool_builder.create_pool`` or
``AgentPipeline``. The strategy is stateless: it is called once during pool
assembly, returns a fully-configured :class:`StrategyAssembly`, and is never
touched again at runtime. Runtime state lives in the assembly's
:class:`TurnRunner`, not in the strategy.

Adding a new pool shape = implementing this ABC + registering it (through
:class:`StrategyComponentFactory`, which surfaces the strategy's
:class:`~modex_agent.scope.runtime_ownership.RuntimeOwnership` as the
probeable manifest — the vocabulary lives in
:mod:`modex_agent.scope.runtime_ownership`, imported downward);
``pool_builder.create_pool`` and ``AgentPipeline`` do not branch on strategy
identity.

Two frozen ``@dataclass`` types carry the assembly contract:

- :class:`PoolAssemblyContext` — input to :meth:`assemble`; ~30
  common-assembly resource fields; strategies must not mutate (frozen).
- :class:`StrategyAssembly` — output of :meth:`assemble`; carries the
  shared pool services plus the per-shape products
  (:class:`ReactMainProducts` / a ``runtime_constructor`` /
  :class:`MainAssembly`).

Both are runtime-object containers per rule 12 (NOT Pydantic ``BaseModel``) —
their fields are live objects with connections and state (``Agent``,
``MessageBroker``, ``LLMProvider``, ``ExternalTransport``), not
serializable values. This is the ADR-0025 D2 判例: runtime-object containers
with cross-module visibility use frozen ``@dataclass``, not ``BaseModel``.

``TurnRunner`` is imported from ``pipeline.turn_runner_abc`` under
``TYPE_CHECKING`` only — this is a type-only ``multi_agent/ -> pipeline/``
dependency. A runtime import would be a cycle (``pipeline/`` already depends
on ``multi_agent/`` at runtime for ``RouteResult``).

See ADR-0025 (D1, D2) for the full decision rationale.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentFactory, ComponentSlot

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.app.config import AppConfig
    from modex_agent.app.models.choice import ModelChoiceRegistry
    from modex_agent.app.models.registry import ModelRegistry
    from modex_agent.commands.models import CommandProcessor
    from modex_agent.control.channel import ControlChannel
    from modex_agent.core.emitter import TurnEventSinkFactory
    from modex_agent.core.inbox import InboxMQ
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.scope import RecordScope
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.core.workspace_root import WorkspaceRootProvider
    from modex_agent.hook.abc import Hook, HookSpec
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.memory.context import ContextManager
    from modex_agent.messaging.agent_messages import AgentMessageRouter
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.communication.peer_resolution import PeerLink
    from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentInstance
    from modex_agent.multi_agent.factory import AgentFactory
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.pool import SessionRetentionPolicy
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.multi_agent.tools import CommunicationTargetStore
    from modex_agent.persistence.managers import WorkspacePersistenceManager
    from modex_agent.persistence.session_registry import SessionRegistry
    from modex_agent.persistence.session_store import SessionStore
    from modex_agent.pipeline.snapshot import PoolDataSnapshot
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
    from modex_agent.plugins.assembly.context import AgentContext, AssemblyContext
    from modex_agent.plugins.assembly.model_assembly import PoolModelAssembly
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_agent.trace.cassette import CassetteRecorder
    from modex_agent.workspace.handle import WorkspaceHandle
    from modex_agent.workspace.resources import WorkspaceManager
    from modex_agent.workspace.scope_path import ScopePath

from modex_agent.scope.assembly_spec import AssemblySpec
from modex_agent.scope.runtime_ownership import RuntimeOwnership, StrategyManifest
from modex_agent.scope.spec import PoolSpec

__all__ = [
    "ExecutionStrategy",
    "ExecutionStrategyRegistry",
    "MainAssembly",
    "PoolAssemblyContext",
    "ReactMainProducts",
    "StrategyAssembly",
    "StrategyComponentFactory",
    "StrategyConfig",
    "SubagentAssembly",
    "default_strategy_registry",
    "strategy_registry_from_components",
]


class StrategyConfig(BaseModel):
    """Empty config schema — a pool shape's configuration lives in the
    scope declaration (strategy name, provider kind, per-agent fields),
    not in slot config. The frozen default ``config_model`` of
    :class:`StrategyComponentFactory`."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class StrategyComponentFactory(ComponentFactory):
    """EXECUTION_STRATEGY slot factory wrapping one strategy singleton.

    The slot's registration face for EVERY strategy — bundled
    (``react`` / ``external``) and third-party alike. Two products, one
    factory:

    - :meth:`create` returns the wrapped strategy instance (strategies
      are stateless singletons — ``assemble_main`` runs once per pool at
      build time, so one instance is shared by every resolution);
    - :meth:`probe` surfaces the :class:`StrategyManifest` — the
      synchronous ownership face the scope compiler and tree validator
      read WITHOUT importing this module (the compile-visible contract,
      ADR-0052 addendum).

    Register through
    :meth:`modex_agent.plugins.loader.PluginRegistrationContext.
    register_execution_strategy` (or ``ComponentRegistry.register``) —
    never wrap a strategy in :class:`~modex_agent.scope.components.
    SimpleFactory` (its probe returns the strategy, not the manifest,
    and fails the slot contract loudly at compile).
    """

    config_model: ClassVar[type[BaseModel]] = StrategyConfig

    def __init__(self, strategy: ExecutionStrategy) -> None:
        self._strategy = strategy

    @property
    def strategy(self) -> ExecutionStrategy:
        """The wrapped strategy singleton (the runtime registry face)."""
        return self._strategy

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> ExecutionStrategy:  # noqa: ARG002
        """Return the wrapped strategy singleton. Ignores config and ctx."""
        return self._strategy

    def probe(self) -> StrategyManifest:
        """Surface the strategy's manifest (name + ownership)."""
        return StrategyManifest(
            name=self._strategy.name,
            ownership=self._strategy.ownership,
        )


class ExecutionStrategy(ABC):
    """Abstract base class for pool-shape recipes (ADR-0025 D1).

    A strategy owns one full pool shape (ReAct graph loop, external CLI
    harness, future shapes). It is **stateless**: called once during pool
    assembly, returns a fully-configured :class:`StrategyAssembly`, and is
    never touched again at runtime. Runtime state lives in the assembly's
    :class:`TurnRunner`.

    Since W5 the runtime is a SLOT PRODUCT: the agent runtime is no
    longer selected by a closed enum branch inside the agent factory.
    Each strategy owns its runtime construction —

    - self-owning shapes (``external``) build their agent + turn runner
      directly and return them as :attr:`StrategyAssembly.main`;
    - native-component shapes return :attr:`StrategyAssembly.
      runtime_constructor` — their own loop builder plugged into the
      shared native assembly (``assemble_native_agent``), reusing the
      full native component resolution (tools/hooks/llm/memory/
      capabilities) with a custom loop ("native components + custom
      loop").

    Subclasses declare:

    - ``name``: unique registry key (e.g. ``"react"``, ``"external"``).
    - ``ownership``: the frozen :class:`RuntimeOwnership` declaration
      (default — the react-like shape: framework owns every component
      face). The contract is enforced, not advisory: registration
      through :class:`StrategyComponentFactory` surfaces it as the
      probeable :class:`StrategyManifest`, and misdeclaration fails at
      compile/boot — an unregistered strategy NAME, a memory-surface or
      root-approval request on a strategy declaring ``needs_memory=False``
      / ``supports_approval=False``, and (via ``owns_context``) the V12
      capability exclusion and the position-default-hook exclusion are
      all compile-time errors (``scope.runtime_ownership``).
    - :meth:`assemble_main`: construct all runtime components this strategy
      needs for the pool's main agent and return a
      :class:`StrategyAssembly`.
    - :meth:`assemble_sub`: assemble a per-invocation subagent of this
      strategy's shape (optional — only external shapes implement it).
    - :meth:`validate_pool_spec`: fail-fast at startup if the pool spec is
      incompatible with this strategy. The base implementation enforces
      the ownership-derived subagent rule; subclasses override to extend
      (e.g. ``external`` additionally requires ``provider_kind``).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique registry key for this strategy (e.g. ``"react"``)."""
        ...

    @property
    def ownership(self) -> RuntimeOwnership:
        """What this strategy's runtime owns (default: the react shape —
        the framework owns every component face)."""
        return RuntimeOwnership()

    @abstractmethod
    async def assemble_main(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Construct all runtime components the pool's MAIN agent needs.

        Called once during pool assembly (``PoolAssembleStage`` /
        ``create_pool``). Returns a fully-configured
        :class:`StrategyAssembly` whose ``turn_runner`` is ready to execute
        turns.
        """
        ...

    async def assemble_sub(
        self,
        ctx: AgentContext,
        deps: AgentMaterializeDeps,
    ) -> SubagentAssembly:
        """Assemble a SUBAGENT of this strategy's shape (per-invocation).

        Called by ``AgentTemplate.materialize`` when a subagent spec selects
        this strategy (a subagent's ``execution_strategy`` field is resolved
        against the strategy registry — it may differ from the pool's main
        strategy). Only strategies with an external subagent shape implement
        this; the default raises because native react subagents are
        constructed directly by the assembly materializer and never reach a
        strategy.

        Ticket 10: the per-invocation data (``parent_session``,
        ``invocation_id``, agent identity, and the per-agent spec
        reference) rides the full-chain :class:`AgentContext` — one
        mechanism, same as every other agent-layer datum (the former
        per-invocation special-case context is deleted). ``deps`` carries
        the per-pool materialize connections (tree, broker, session
        registry, path resolver) — per-pool data, not per-invocation
        data.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support subagent assembly")

    def validate_pool_spec(self, pool: PoolSpec) -> None:
        """Fail-fast at startup if the declared pool is incompatible.

        The base implementation derives the subagent rule from
        :attr:`ownership`: a strategy that declares
        ``supports_subagents=False`` rejects pools with subagent
        templates. Subclasses override to EXTEND (calling
        ``super().validate_pool_spec(pool)`` first), never to weaken.

        Raises ``ValueError`` (or a more specific subtype) on violation.
        """
        if not self.ownership.supports_subagents and len(pool.agents) > 1:
            raise ValueError(
                f"Pool {pool.name!r}: execution_strategy {self.name!r} "
                "does not support subagents"
            )


@dataclass(frozen=True)
class PoolAssemblyContext:
    """Input to :meth:`ExecutionStrategy.assemble_main` — common-assembly resources.

    Carries every resource the common assembly phase (``pool_builder``)
    produces that a strategy might read. Strategies must not mutate this
    object (frozen). Field set is bounded by what common assembly produces;
    new common resources extend it, new strategy-specific resources do not.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Strategies must not mutate (frozen).

    Deployment-owned objects the framework reads through typed seams
    (W4a): ``workspace_handle`` (:class:`~modex_agent.workspace.handle.WorkspaceHandle`),
    ``workspace_resolver`` (:class:`~modex_agent.workspace.resources.WorkspaceManager`),
    ``persistence``
    (:class:`~modex_agent.persistence.managers.WorkspacePersistenceManager`),
    and ``model_assembly``
    (:class:`~modex_agent.plugins.assembly.model_assembly.PoolModelAssembly`
    — the pool's model-universe seam, also consumed by the bundled ``multi``
    LLM factory). The model-universe fields (``bot_model_config``,
    ``model_choice_registry``) are typed with the framework model-registry
    types (W4b): ``ModelRegistry`` and ``ModelChoiceRegistry`` from
    :mod:`modex_agent.app.models`.
    """

    # Required: pool identity and config
    pool_name: str
    pool_spec: PoolSpec
    """The DECLARED pool (``modex_agent.scope.spec.PoolSpec``) — the
    declaration road's single pool face; strategies read the root agent
    via ``pool_spec.root_agent``."""
    project_dir: Path
    data_dir: Path

    broker: MessageBroker
    inbox_server: InboxMQ
    agent_bus: AgentMessageBus

    output_adapter: OutputAdapter

    safety: RuntimeSafetyPolicy
    retention: SessionRetentionPolicy

    registry: TurnSessionRegistry | None = None
    """Legacy field inherited from the example build — always ``None`` on
    every production path (``create_pool`` passes ``None``; no strategy or
    stage reads it). Optional so hand-built contexts need not fabricate a
    value; removal awaits a consumer that actually owns turn-session
    state here."""

    peer_links: tuple[PeerLink, ...] = ()
    """The pool's declared peer links (the env-spec agent-pool map reads
    the peer roots' declared names)."""
    workspace_handle: WorkspaceHandle | None = None
    workspace_resolver: WorkspaceManager | None = None
    scope_path: ScopePath | None = None
    """The pool's resolved :class:`~modex_agent.workspace.scope_path.ScopePath`
    (workspace root + pool name) — the single scope-path carrier for the
    pool's assembly consumers (capability supplies build scope-path-aware
    services on it; subagent materialization threads the SAME object).
    ``None`` for hand-built contexts (framework tests)."""

    emitter_factory: TurnEventSinkFactory | None = None

    app_config: AppConfig | None = None
    persistence: WorkspacePersistenceManager | None = None

    mcp_registry: McpConnectionRegistry | None = None

    shared_hooks: list[Hook] = field(default_factory=list)
    shared_hook_runner: HookRunner | None = None
    shared_interceptor_chain: InterceptorChain | None = None

    session_registry: SessionRegistry | None = None
    session_store: SessionStore | None = None

    bot_model_config: ModelRegistry | None = None
    """The deployment's resolved model registry (``ModelRegistry``) - read by
    deployment LLM factories (e.g. the per-turn choice hook's factory);
    framework assembly reads the model universe through the injected
    :class:`~modex_agent.plugins.assembly.model_assembly.PoolModelAssembly`
    (``model_assembly`` below)."""
    model_choice_registry: ModelChoiceRegistry | None = None
    """The per-turn model-choice registry - read by deployment factories only."""
    model_assembly: PoolModelAssembly | None = None
    """The pool's model-universe seam - threaded by ``create_pool`` and
    consumed by the bundled ``multi`` LLM_PROVIDER factory
    (``selection_provider()``). ``None`` for hand-built contexts
    (framework tests)."""

    record_scope: RecordScope | None = None
    """The pool's storage isolation scope (deployment may subclass
    :class:`~modex_agent.core.scope.RecordScope` with business dimensions,
    e.g. a per-pool dimension). Consumed by the promoted persistence
    backend factories; ``None`` → a default ``RecordScope()``."""

    root_system_prompt: str = ""
    """The pool root's RESOLVED system prompt (the SYSTEM_PROMPT_PROVIDER
    slot product, resolved once by the orchestrator). Self-owning
    strategies that build their main runtime directly (``external``)
    thread it onto the main descriptor; native-component strategies read
    the provider through Stage 4 instead. Empty for hand-built contexts."""

    workspace_resources: Any | None = None
    """The workspace's materialized resource bundle (deployment ``R``) —
    the same bundle threaded onto the assembly-context workspace layer.
    Strategies dispatching the declared HOOK roster directly (external)
    need it for the dispatch context chain. ``None`` for hand-built
    contexts."""

    control_origin: str = ""
    """The deployment HTTP listener origin (``MODEX_CONTROL_ORIGIN`` source) —
    the env-spec templates built on the context chain (the ``native_env``
    hook factory) read it. Empty for framework/hand-built contexts (the
    env var is still emitted, just empty)."""

    command_processor: CommandProcessor | None = None
    control_channel: ControlChannel | None = None

    pool_data: PoolDataSnapshot | None = None

    on_session_start: Callable[[str], Awaitable[None]] | None = None
    on_session_end: Callable[[str], Awaitable[None]] | None = None

    router: AgentMessageRouter | None = None

    assembly_deps: PoolAssemblyDeps | None = None
    component_registry: ComponentRegistry | None = None
    assembly_spec: AssemblySpec | None = None


@dataclass(frozen=True)
class ReactMainProducts:
    """React-only main-assembly side products (W5).

    Owned by :class:`~modex_agent.plugins.assembly.strategies.react.
    ReactExecutionStrategy`; consumed only by the react wiring points
    (Stage 4's provider wrap + ``wire_main_pipeline``'s cassette flush
    hook). Runtime-object container per rule 12 — NOT Pydantic.
    """

    cassette_recorder: CassetteRecorder | None = None
    component_hook_specs: tuple[HookSpec, ...] = ()


@dataclass(frozen=True)
class MainAssembly:
    """A strategy-built MAIN runtime (W5) — symmetric with SubagentAssembly.

    Self-owning strategies (``external``, third-party loop shapes) build
    their agent + turn runner + pipeline directly in
    :meth:`ExecutionStrategy.assemble_main` and return them here; the
    orchestrator (``create_pool``) registers the pair into the pool.
    Runtime-object container per rule 12 — NOT Pydantic.
    """

    descriptor: AgentDescriptor
    instance: AgentInstance


@dataclass(frozen=True)
class StrategyAssembly:
    """Output of :meth:`ExecutionStrategy.assemble_main` — runtime-object container.

    Carries what the pool builder and pipeline need from the strategy:
    the shared pool services and, per shape, the strategy-specific
    products — ``react_products`` (react-only side products),
    ``runtime_constructor`` (a native-component custom loop), or ``main``
    (a strategy-built self-owning runtime). See the field docs for which
    strategies fill which field.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    ``None`` fields are strategy-specific; consumers gate on the
    strategy's :attr:`~ExecutionStrategy.ownership` rather than
    ``is None`` checks.
    """

    tool_manager: ToolManager | None = None
    """The main agent's base tool manager (react builds the empty base;
    self-owning shapes leave ``None`` — the fallback manager applies)."""

    context_manager: ContextManager | None = None
    """The main agent's context manager (react resolves it from pool data
    or its fallback; shapes declaring ``owns_context`` leave ``None``)."""

    notification_service: AgentNotificationService | None = None
    """Pool notification service override; the orchestrator's supplied
    service wins (``PoolAssembleStage``)."""

    target_store: CommunicationTargetStore | None = None
    """Pool binding store (``PoolAssembleStage`` propagates it into
    ``PoolRuntimeDeps``)."""

    control_channel: ControlChannel | None = None
    """Pool control channel (``PoolAssembleStage`` propagates it into
    ``PoolRuntimeDeps``)."""

    root_provider: WorkspaceRootProvider | None = None
    """The pool's workspace-root provider — consumed by the SHARED
    machinery (``PoolAssembleStage`` enriches ``PoolRuntimeDeps`` for
    every strategy; the sandbox guard + approval classifier read it)."""

    react_products: ReactMainProducts | None = None
    """React-only side products (cassette recorder, component hook
    specs). ``None`` for non-react shapes."""

    runtime_constructor: AgentFactory | None = None
    """A third-party native-component strategy's own loop builder (W5).
    When set, Stage 4 threads this constructor into
    ``assemble_native_agent`` instead of the default react factory —
    "native components + custom loop". ``None`` for react (the default
    react factory applies) and for self-owning shapes (no Stage 4)."""

    main: MainAssembly | None = None
    """A strategy-built main runtime (external / self-owning shapes).
    ``None`` for native-component shapes whose main is constructed by
    Stage 4 (react)."""


@dataclass(frozen=True)
class SubagentAssembly:
    """Output of :meth:`ExecutionStrategy.assemble_sub` — subagent carrier.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``. The
    caller (``AgentTemplate.materialize``) registers both halves into the
    pool via ``pool.register_resident(descriptor, instance)``.
    """

    descriptor: AgentDescriptor
    instance: AgentInstance


class ExecutionStrategyRegistry:
    """Process-scoped, write-once-read-many registry of :class:`ExecutionStrategy`.

    Services derive this runtime lookup from the ``EXECUTION_STRATEGY`` slot in
    their :class:`ComponentRegistry`; they do not register a parallel strategy
    set by hand. The framework-only :func:`default_strategy_registry` remains
    an empty registry for isolated callers.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, ExecutionStrategy] = {}

    def register(self, strategy: ExecutionStrategy) -> None:
        """Register a strategy. Raises ``ValueError`` on duplicate name."""
        if strategy.name in self._strategies:
            raise ValueError(f"Duplicate execution strategy: {strategy.name}")
        self._strategies[strategy.name] = strategy

    def resolve(self, name: str) -> ExecutionStrategy:
        """Resolve a strategy by name. Raises ``ValueError`` on unknown name."""
        if name not in self._strategies:
            raise ValueError(
                f"Unknown execution strategy: {name!r}. Registered: {sorted(self._strategies)}"
            )
        return self._strategies[name]

    def names(self) -> list[str]:
        """Return sorted list of registered strategy names."""
        return sorted(self._strategies)


def default_strategy_registry() -> ExecutionStrategyRegistry:
    """Return an empty registry.

    This is the framework-only entry point for isolated callers. Applications
    derive their registry from ``ComponentRegistry`` instead.
    """
    return ExecutionStrategyRegistry()


def strategy_registry_from_components(
    registry: ComponentRegistry,
) -> ExecutionStrategyRegistry:
    """Derive the runtime strategy registry from the slot registrations.

    Every registration is a :class:`StrategyComponentFactory` (the slot's
    one factory face — its probe surfaces the manifest, its create
    returns the strategy); a non-conforming registration is skipped with
    a warning instead of entering the runtime registry.
    """
    strategy_registry = ExecutionStrategyRegistry()
    factories = registry.factories(ComponentSlot.EXECUTION_STRATEGY)
    for name, factory in factories.items():
        # Extension boundary: strategy registration accepts heterogeneous
        # plugin factories; the slot contract is StrategyComponentFactory.
        if not isinstance(factory, StrategyComponentFactory):
            logger.warning(
                "Skipping execution strategy component %r: expected "
                "StrategyComponentFactory, got %s",
                name,
                type(factory).__name__,
            )
            continue
        strategy_registry.register(factory.strategy)
    return strategy_registry
