"""ExecutionStrategy ABC + registry + assembly contract (ADR-0025).

A pool shape (ReAct graph loop, external CLI harness, future shapes) is
decided by an explicit :class:`ExecutionStrategy` ABC, not by scattered
``if execution_strategy ==`` branches in ``pool_builder.create_pool`` or
``AgentPipeline``. The strategy is stateless: it is called once during pool
assembly, returns a fully-configured :class:`StrategyAssembly`, and is never
touched again at runtime. Runtime state lives in the assembly's
:class:`TurnRunner`, not in the strategy.

Adding a new pool shape = implementing this ABC + registering it;
``pool_builder.create_pool`` and ``AgentPipeline`` do not branch on strategy
identity.

Two frozen ``@dataclass`` types carry the assembly contract:

- :class:`PoolAssemblyContext` — input to :meth:`assemble`; ~30
  common-assembly resource fields; strategies must not mutate (frozen).
- :class:`StrategyAssembly` — output of :meth:`assemble`; carries the
  ``Agent``, the ``TurnRunner``, common services, react-only collaborators,
  external-only collaborators, and ``extra_cleanup`` hooks.

Both are runtime-object containers per rule 12 (NOT Pydantic ``BaseModel``) —
their fields are live objects with connections and state (``Agent``,
``MessageBroker``, ``LLMProvider``, ``StreamingProviderBackend``), not
serializable values. This is the ADR-0025 D2 判例: runtime-object containers
with cross-module visibility use frozen ``@dataclass``, not ``BaseModel``.

``TurnRunner`` is imported from ``pipeline.turn_runner_abc`` under
``TYPE_CHECKING`` only — this is a type-only ``multi_agent/ -> pipeline/``
dependency. A runtime import would be a cycle (``pipeline/`` already depends
on ``multi_agent/`` at runtime for ``RouteResult``).

See ADR-0025 (D1, D2) for the full decision rationale.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.agents.external.agent import StreamingProviderBackend
    from modex_agent.agents.external.session_store import ExternalSessionMapStore
    from modex_agent.commands.models import CommandProcessor
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.agent import Agent
    from modex_agent.core.context import ContextManager
    from modex_agent.core.emitter import ContentEmitter
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.core.session_store import SessionStore
    from modex_agent.core.skills.manager import SkillManager
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.hook.abc import HookSpec
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.hook.runner import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.ioc.configs.app import AppConfig
    from modex_agent.memory.consolidation.dream_engine import DreamEngine
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.communication.service import AgentCommunicationService
    from modex_agent.multi_agent.inbox.server import InboxMQ
    from modex_agent.multi_agent.pool import SessionRetentionPolicy
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.multi_agent.pool_config.specs import PoolSpec
    from modex_agent.multi_agent.router import AgentMessageRouter
    from modex_agent.multi_agent.tools import CommunicationTargetStore
    from modex_agent.pipeline.adapters import OutputAdapter
    from modex_agent.pipeline.snapshot import PoolDataSnapshot
    from modex_agent.pipeline.turn_runner_abc import TurnRunner
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
    from modex_agent.runtime.store import JsonFileTodoStore
    from modex_agent.tools.mcp.manager import MCPClientManager
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_agent.tools.terminal.managers import BaseTerminalManager
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
    from modex_agent.trace.cassette import CassetteRecorder

__all__ = [
    "ExecutionStrategy",
    "ExecutionStrategyRegistry",
    "PoolAssemblyContext",
    "StrategyAssembly",
    "default_strategy_registry",
]


class ExecutionStrategy(ABC):
    """Abstract base class for pool-shape recipes (ADR-0025 D1).

    A strategy owns one full pool shape (ReAct graph loop, external CLI
    harness, future shapes). It is **stateless**: called once during pool
    assembly, returns a fully-configured :class:`StrategyAssembly`, and is
    never touched again at runtime. Runtime state lives in the assembly's
    :class:`TurnRunner`.

    Subclasses declare:

    - ``name``: unique registry key (e.g. ``"react"``, ``"external"``).
    - ``supports_subagents``: whether this shape permits subagent templates
      (default ``True``; ``external`` overrides to ``False``).
    - ``requires_main_agent_tools``: whether the pool builder must register
      the ``task`` communication tool on the main agent (default
      ``True``; ``external`` overrides to ``False`` since its main
      agent has no tool surface).
    - :meth:`assemble`: construct all runtime components this strategy needs
      and return a :class:`StrategyAssembly`.
    - :meth:`validate_pool_spec`: fail-fast at startup if the pool spec is
      incompatible with this strategy (e.g. ``external`` rejects
      subagents and requires ``provider_kind``).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique registry key for this strategy (e.g. ``"react"``)."""
        ...

    @property
    def supports_subagents(self) -> bool:
        """Whether this strategy permits subagent templates (default ``True``)."""
        return True

    @property
    def requires_main_agent_tools(self) -> bool:
        """Whether the pool builder registers ``task`` on the main
        agent (default ``True``). ``external`` overrides to ``False``
        — its main agent has no tool surface.
        """
        return True

    @abstractmethod
    async def assemble(self, ctx: PoolAssemblyContext) -> StrategyAssembly:
        """Construct all runtime components this strategy needs.

        Called once during pool assembly. Returns a fully-configured
        :class:`StrategyAssembly` whose ``turn_runner`` is ready to execute
        turns.
        """
        ...

    @abstractmethod
    def validate_pool_spec(self, spec: PoolSpec) -> None:
        """Fail-fast at startup if ``spec`` is incompatible with this strategy.

        Called before :meth:`assemble`. Raises ``ValueError`` (or a more
        specific subtype) on incompatibility.
        """
        ...


@dataclass(frozen=True)
class PoolAssemblyContext:
    """Input to :meth:`ExecutionStrategy.assemble` — common-assembly resources.

    Carries every resource the common assembly phase (``pool_builder``)
    produces that a strategy might read. Strategies must not mutate this
    object (frozen). Field set is bounded by what common assembly produces;
    new common resources extend it, new strategy-specific resources do not.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``.
    Strategies must not mutate (frozen).

    Bot-side-only types (``workspace_handle``, ``workspace_resolver``,
    ``persistence``, ``bot_model_config``, ``model_choice_registry``,
    ``transcript_store``, ``kb_provider``) are typed as ``Any`` — these are bot-layer objects
    the framework does not import; ``Any`` is the documented escape hatch at
    the framework/bot boundary.
    """

    # Required: pool identity and config
    pool_name: str
    pool_spec: PoolSpec
    project_dir: Path
    data_dir: Path

    broker: MessageBroker
    inbox_server: InboxMQ
    agent_bus: AgentMessageBus

    output_adapter: OutputAdapter

    safety: RuntimeSafetyPolicy
    retention: SessionRetentionPolicy

    registry: TurnSessionRegistry

    workspace_handle: Any | None = None
    workspace_resolver: Any | None = None

    emitter_factory: Callable[[str], ContentEmitter[Any]] | None = None

    app_config: AppConfig | None = None
    persistence: Any | None = None

    mcp_registry: McpConnectionRegistry | None = None

    shared_hooks: list[HookSpec] = field(default_factory=list)
    shared_hook_runner: HookRunner | None = None
    shared_interceptor_chain: InterceptorChain | None = None

    session_registry: SessionRegistry | None = None
    session_store: SessionStore | None = None

    bot_model_config: Any | None = None
    model_choice_registry: Any | None = None

    command_processor: CommandProcessor | None = None
    control_channel: InMemoryControlChannel | None = None

    pool_data: PoolDataSnapshot | None = None
    transcript_store: Any | None = None
    kb_provider: Any | None = None

    on_session_start: Callable[[str], Awaitable[None]] | None = None
    on_session_end: Callable[[str], Awaitable[None]] | None = None

    router: AgentMessageRouter | None = None

    assembly_deps: PoolAssemblyDeps | None = None


@dataclass(frozen=True)
class StrategyAssembly:
    """Output of :meth:`ExecutionStrategy.assemble` — runtime-object container.

    Carries everything the pool builder and pipeline need from the strategy:
    the ``Agent``, the :class:`TurnRunner`, common services, react-only
    collaborators (``None`` for external), external-only collaborators
    (``None`` for react), and ``extra_cleanup`` hooks.

    Runtime-object container per rule 12 — NOT Pydantic ``BaseModel``. ``None``
    fields are strategy-specific; consumers gate on
    ``strategy.requires_main_agent_tools`` rather than ``is None`` checks.

    Transitional (tickets 3-4): ``agent``, ``turn_runner``,
    ``notification_service``, ``communication_service``, ``target_store`` may
    all be ``None``. React fills ``agent``/``turn_runner`` ``None`` in ticket 3
    (the agent instance + turn_runner are created downstream by the factory +
    pipeline); ticket 5 makes react fill ``turn_runner`` once the pipeline
    accepts a runner parameter. The common-service trio
    (``notification_service``/``communication_service``/``target_store``) is
    built by ``pool_builder`` for both strategies in tickets 3-4; ticket 5/6
    may move them into ``assemble()`` and make them required again.

    The react-only side-product fields ``cassette_recorder``, ``todo_store``,
    ``root_provider`` are also transitional: ``ReactExecutionStrategy.assemble()``
    fills them so ``pool_builder`` can finish post-assembly wiring
    (cassette flush hook, subagent ``AgentMaterializeDeps``, approval root)
    without re-running the build helpers. Ticket 6 moves the helpers into the
    strategy and these fields leave the assembly contract.

    The external-only transitional field ``external_deps`` carries the
    deps dict that ``ExternalAwareFactory`` reads to build an
    ``ExternalAgent``. ``ExternalExecutionStrategy.assemble()``
    fills it; ``None`` for react. Ticket 6 eliminates this field when
    strategies build agents directly (the factory dispatch branch is deleted
    and the strategy owns agent construction).
    """

    # Transitional: see class docstring.
    agent: Agent[Any] | None = None
    turn_runner: TurnRunner | None = None
    notification_service: AgentNotificationService | None = None
    communication_service: AgentCommunicationService | None = None
    target_store: CommunicationTargetStore | None = None

    provider: LLMProvider | None = None
    tool_manager: ToolManager | None = None
    skill_manager: SkillManager | None = None
    mcp_manager: MCPClientManager | None = None
    terminal_manager: BaseTerminalManager | None = None
    context_manager: ContextManager | None = None
    dream_engine: DreamEngine | None = None
    dream_interval: float | None = None
    command_processor: CommandProcessor | None = None
    control_channel: InMemoryControlChannel | None = None

    backend: StreamingProviderBackend | None = None
    session_map_store: ExternalSessionMapStore | None = None

    # Transitional react-only side products (see class docstring). Filled by
    # ``ReactExecutionStrategy.assemble()``; ``None`` for external.
    cassette_recorder: CassetteRecorder | None = None
    todo_store: JsonFileTodoStore | None = None
    root_provider: WorkspaceRootProvider | None = None

    # Transitional external-only deps dict (see class docstring). Filled by
    # ``ExternalExecutionStrategy.assemble()``; ``None`` for react.
    external_deps: dict[str, Any] | None = None

    extra_cleanup: tuple[Callable[[], Awaitable[None]], ...] = ()


class ExecutionStrategyRegistry:
    """Process-scoped, write-once-read-many registry of :class:`ExecutionStrategy`.

    ``BotService.initialize()`` registers shipped strategies (``react``,
    ``external``) before any pool is created. The framework ships a
    :func:`default_strategy_registry` factory that returns an empty registry;
    shipped strategies register themselves in their own tickets (3 and 4).
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

    Shipped strategies (``react``, ``external``) register themselves in
    later tickets once their classes exist. This factory is the framework-only
    entry point; business layers may override by constructing their own
    registry and registering a custom strategy set.
    """
    return ExecutionStrategyRegistry()
