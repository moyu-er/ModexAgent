"""AgentMaterializeDeps — bundled dependencies for AgentTemplate.materialize.

Replaces the ~30 scattered constructor parameters that
AgentCommunicationService used to take to tentatively support subagent
construction. Constructed once at pool wiring time and passed into
AgentTemplate.materialize per call (ADR-0015 D5).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.adapters.output import OutputAdapter
    from modex_agent.core.llm import LLMProvider
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.core.session_id import SessionIdFactory
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.hook.notification import AgentNotificationService
    from modex_agent.memory.registry import MemoryStoreRegistry
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent.bus import AgentMessageBus
    from modex_agent.multi_agent.context_fork import ContextForkBuilder
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        PoolAssemblyContext,
    )
    from modex_agent.multi_agent.factory import AgentFactory
    from modex_agent.multi_agent.inbox.consumer import InboxConsumer
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.plugins.capability import CapabilitySupply
    from modex_agent.plugins.registry import ComponentRegistry
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
    from modex_agent.workspace import WorkspaceManager
    from modex_agent.workspace.scope_path import ScopePath
    from modex_graph.context import GraphContext

from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.emitter import ContentEmitter


class AgentMaterializeDeps:
    """Bundled construction deps for AgentTemplate.materialize."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        pool: AgentPool,
        session_factory: SessionIdFactory,
        broker: MessageBroker,
        tree: SessionTreeManager,
        safety: RuntimeSafetyPolicy | None = None,
        llm_model: str | None = None,
        llm_temperature: float = 0.7,
        llm_max_output_tokens: int | None = None,
        llm_reasoning_effort: ReasoningEffort = ReasoningEffort.NONE,
        llm_model_info: ModelInfo | None = None,
        llm_provider: LLMProvider | None = None,
        project_dir: Path | None = None,
        notification_service: AgentNotificationService | None = None,
        inbox_consumer: InboxConsumer | None = None,
        agent_bus: AgentMessageBus | None = None,
        output_adapter_factory: Callable[[], OutputAdapter] | None = None,
        root_provider: WorkspaceRootProvider | None = None,
        session_registry: SessionRegistry | None = None,
        on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
        context_fork_builder: ContextForkBuilder | None = None,
        scope_path: ScopePath | None = None,
        workspace_manager: WorkspaceManager | None = None,
        workspace_resources: Any | None = None,
        mcp_registry: McpConnectionRegistry | None = None,
        execution_strategy: ExecutionStrategy | None = None,
        strategy_registry: ExecutionStrategyRegistry | None = None,
        data_dir: Path | None = None,
        app_config: Any | None = None,
        persistence: Any | None = None,
        memory_store_registry: MemoryStoreRegistry | None = None,
        emitter_factory: Callable[[str], ContentEmitter] | None = None,
        control_origin: str = "",
        component_registry: ComponentRegistry | None = None,
        pool_assembly_ctx: PoolAssemblyContext | None = None,
        default_llm_provider: str = "default",
        graph_context_resolver: Callable[[int], GraphContext[Any] | None] | None = None,
        capability_supply: Mapping[str, CapabilitySupply] = MappingProxyType({}),
    ) -> None:
        self.agent_factory = agent_factory
        self.pool = pool
        self.session_factory = session_factory
        self.broker = broker
        self.tree = tree
        self.safety = safety
        self.llm_model = llm_model
        self.llm_temperature = llm_temperature
        self.llm_max_output_tokens = llm_max_output_tokens
        self.llm_reasoning_effort = llm_reasoning_effort
        self.llm_model_info = llm_model_info
        self.llm_provider = llm_provider
        self.project_dir = project_dir
        self.notification_service = notification_service
        self.inbox_consumer = inbox_consumer
        self.agent_bus = agent_bus
        self.output_adapter_factory = output_adapter_factory
        self.root_provider = root_provider
        self.session_registry = session_registry
        self.on_subagent_created = on_subagent_created
        self.context_fork_builder = context_fork_builder
        self.scope_path = scope_path
        self.workspace_manager = workspace_manager
        self.workspace_resources = workspace_resources
        self.mcp_registry = mcp_registry
        self.execution_strategy = execution_strategy
        self.strategy_registry = strategy_registry
        self.data_dir = data_dir
        self.app_config = app_config
        self.persistence = persistence
        self.memory_store_registry = memory_store_registry
        self.emitter_factory = emitter_factory
        self.control_origin = control_origin
        self.component_registry = component_registry
        self.pool_assembly_ctx = pool_assembly_ctx
        self.default_llm_provider = default_llm_provider
        self.graph_context_resolver = graph_context_resolver
        self.capability_supply = capability_supply

    safety: RuntimeSafetyPolicy | None
    llm_model: str | None
    # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
    # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
    # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
    # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。收敛目标见
    # docs/superpowers/plans/2026-07-03-bot-multi-model.md §框架配置收敛后续。
    llm_temperature: float
    llm_max_output_tokens: int | None
    llm_reasoning_effort: ReasoningEffort
    llm_model_info: ModelInfo | None
    llm_provider: LLMProvider | None
    project_dir: Path | None
    notification_service: AgentNotificationService | None
    inbox_consumer: InboxConsumer | None
    agent_bus: AgentMessageBus | None
    output_adapter_factory: Callable[[], OutputAdapter] | None
    root_provider: WorkspaceRootProvider | None
    session_registry: SessionRegistry | None
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None
    context_fork_builder: ContextForkBuilder | None
    scope_path: ScopePath | None
    workspace_resources: Any | None
    """The workspace's materialized resource bundle (business ``R``).

    Threads the workspace layer onto subagent assembly: factories
    resolving workspace-scoped resources (e.g. the bot ``kb`` tool's
    KbProvider) read it off the assembly context chain, same as the
    main-agent road. ``None`` on non-workspace test contexts.
    """
    workspace_manager: WorkspaceManager | None
    """Workspace manager for per-turn workspace binding (``set_pool_context``).

    Injected post-build via ``turn_runner.set_pool_context`` so the turn
    runner resolves the ACTIVE workspace during each turn. The
    ``_create_with_emitter`` wrapper in ``bot/service/pool/agent_factory.py``
    handles main agents and react subagents; external subagents (which
    bypass the wrapper) are wired via the shared
    ``_inject_emitter_and_pool_context`` helper called from
    ``_materialize_external`` in ``template.py``. ``None`` for framework
    tests / non-bot callers."""
    mcp_registry: McpConnectionRegistry | None
    execution_strategy: ExecutionStrategy | None
    """The POOL's main-agent execution strategy (resolved from the main
    spec's ``execution_strategy`` field). Kept for callers that need the
    pool-level strategy; subagent assembly must NOT use this — a subagent
    selects its own strategy via ``strategy_registry`` below."""
    strategy_registry: ExecutionStrategyRegistry | None
    """Process-scoped strategy registry (react + external + custom shapes).

    ``AgentTemplate.materialize`` resolves an EXTERNAL subagent's OWN
    strategy here — a subagent's ``execution_strategy`` field may differ
    from its pool's main strategy (e.g. react main + external sub).
    Injected by ``create_pool`` from the service-level registry; ``None``
    for framework tests (react-only paths never read it)."""
    data_dir: Path | None
    """Per-pool data directory — needed by the external sub path to resolve
    ``inbox_root``. Injected by ``create_pool``; ``None`` for framework tests."""
    app_config: Any | None
    """Bot-layer ``AppConfig`` — needed by the external sub path to build the
    session map store. Typed as ``Any`` (framework does not import bot config).
    ``None`` for framework tests / non-bot callers."""
    persistence: Any | None
    """Bot-layer persistence config — needed by the external sub path to build
    the session map store (FILE vs SQLite). Typed as ``Any``. ``None`` for
    framework tests / non-bot callers."""
    memory_store_registry: MemoryStoreRegistry | None
    """Main agent's ``MemoryStoreRegistry``, threaded to native subagent
    ``build_session_only_memory`` so subagents share the workspace's SQLite
    backend (or file backend) instead of defaulting to a separate
    ``DefaultMemoryStoreRegistry``. ``None`` for framework tests / non-bot
    callers — falls back to file-based per-workspace registry."""
    emitter_factory: Callable[[str], ContentEmitter] | None
    """WebUI (or other channel) emitter factory for transcript persistence.

    Injected post-build via ``turn_runner.set_emitter_factory``. The
    ``_create_with_emitter`` wrapper in ``bot/service/pool/agent_factory.py``
    handles main agents and react subagents; external subagents (which
    bypass the wrapper) are wired via the shared
    ``_inject_emitter_and_pool_context`` helper called from
    ``_materialize_external`` in ``template.py``. Both paths converge on
    the same ``set_emitter_factory`` ABC method (architecture rule 15)."""
    control_origin: str
    """Bot HTTP listener origin (e.g. ``http://127.0.0.1:21800``).

    Surfaced as ``MODEX_CONTROL_ORIGIN`` in the native subagent env spec so
    ``modexctl`` can locate the bot's control API. Injected by the business
    layer (``build_control_origin`` in ``bot.config.webui_config``) via
    ``create_pool``; empty string when not configured (framework tests,
    non-bot callers)."""
    component_registry: ComponentRegistry | None
    pool_assembly_ctx: PoolAssemblyContext | None
    default_llm_provider: str
    graph_context_resolver: Callable[[int], GraphContext[Any] | None] | None
    """Graph-context resolver for graph-mode per-turn configuration.

    Threaded from ``create_pool`` (the same lazy closure the main pipeline
    gets) so ``AgentTemplate.materialize`` can wire the graph turn-config
    trio (binding store + resolver + configurators) onto every materialized
    subagent via the shared ``wire_graph_turn_config`` — a graph-referenced
    lazy subagent leaf then receives its ``deliver`` tool on graph turns
    (SPEC §4 axis 3). ``None`` for framework tests / graph-less callers —
    the wiring is skipped, matching the main-pipeline guard."""
    capability_supply: Mapping[str, CapabilitySupply]
    """Pool-level capability supply (SPEC §7.1) — the SAME aggregated
    mapping ``PoolAssembleStage`` lands on ``PoolRuntimeDeps``;
    ``AgentTemplate.materialize`` threads it onto the per-subagent
    ``PoolRuntimeDeps`` so capability consumers on the subagent path read
    one pool-wide face. Empty for framework tests / pools without
    capabilities."""
