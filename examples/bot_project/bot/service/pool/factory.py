"""Pool factory - IOC-style factory that builds one PoolInstance from PoolSpec.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Each build step
is a focused method.  Convention over configuration: config drives behaviour;
methods read from PoolSpec / MainAgentSpec with sensible defaults.  No giant
if-else chains.

Ticket 6 (ADR-0025): the strategy-specific ``_build_*`` helpers
(``_build_llm_provider``, ``_build_terminal_manager``, ``_build_tools``,
``_build_skill_manager``, ``_resolve_cassette_config``,
``_fallback_context_manager``, ``_cell_sessions_dir``) moved into the shared
:class:`bot.service._assembly_helpers._PoolAssemblyMixin`, inherited by both
:class:`ReactExecutionStrategy` and :class:`ExternalExecutionStrategy`.
``create_pool`` is now strategy-agnostic: it resolves the strategy, calls
``strategy.assemble(ctx)``, and runs the common post-assembly wiring
(register main agent, communication, pipeline construction via factory).
Both react and external pools follow the same code path here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.config.memory_defaults import subagent_memory
from bot.config.webui_config import build_control_origin
from bot.scope import BotRecordScope
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.hook import HookRunner
from modex_agent.hook.notification import AgentNotificationService
from modex_agent.ioc.factories.session_tree import build_session_tree_stores
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.messaging.broker_bridge import BrokerBridgeService, OutputRoute
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps, PoolStore
from modex_agent.multi_agent.pool_config.specs import PoolSpec
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import TaskDispatchTool
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot

from .._assembly_helpers import _resolved_or_placeholder
from ..builders import build_inbox, resolve_system_prompt
from ..external_strategy import ProviderUnavailableError
from .agent_factory import (
    _build_agent_factory,
    _cell_sessions_dir,
    _resolve_trace_enabled,
    _WorkspaceEmitterFactory,
)
from .assembly_context import (
    _build_assembly_context,
    _fallback_context_manager,
)
from .communication import (
    UserNoticeCleanupHook,
    _build_communication,
)
from .external_subagent import _maybe_build_external_subagent_builder
from .memory_defaults import ensure_long_term_defaults
from .pipeline_wiring import _wire_main_pipeline
from .pool_construction import (
    _build_agent_pool,
    _register_main_agent,
)
from .strategy_registry import _default_strategy_registry

if TYPE_CHECKING:
    # ``WorkspaceHandle`` / ``WorkspaceResolverCell`` live in the bundle,
    # which is imported by BotService via this module; deferring them to
    # TYPE_CHECKING keeps the import graph acyclic. Runtime references
    # (``WorkspaceHandleRootProvider``) are imported lazily inside
    # ``create_pool`` for the same reason.
    from bot.kb.provider import KbProvider
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.handle import (
        WorkspaceHandle,
        WorkspaceResolverCell,
    )
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
    )
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_graph.context import GraphContext

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


def _make_graph_context_resolver(
    workspace_resolver: WorkspaceResolverCell,
) -> Callable[[int], GraphContext[Any] | None]:
    """Build a lazy graph-context resolver closure for the main pipeline.

    Defers ``PoolWorkspaceResources`` resolution + ``graph_orchestrator``
    dereference to invocation time so the closure stays robust against
    late workspace materialization (the cell is filled after pool creation)
    and orchestrator LRU eviction (F6-verified pattern).
    """

    def resolve(gid: int) -> GraphContext[Any] | None:
        resources = workspace_resolver.resolve_workspace()
        orchestrator = resources.graph_orchestrator
        if orchestrator is None:
            return None
        return orchestrator.get_graph_context(gid)

    return resolve


async def create_pool(
    pool_name: str,
    pool_spec: PoolSpec,
    assembly_deps: PoolAssemblyDeps,
    *,
    project_dir: Path,
    data_dir: Path,
    broker: Any,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    im_ui: Any,
    shared_hooks: list,
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    control_channel: InMemoryControlChannel | None = None,
    command_processor: Any = None,
    pool_data: PoolDataSnapshot | None = None,
    workspace_handle: WorkspaceHandle | None = None,
    workspace_resolver: WorkspaceResolverCell | None = None,
    emitter_factory: Callable[[str], ContentEmitter] | None = None,
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
    transcript_store: TranscriptStore | None = None,
    bot_model_config: BotModelConfig | None,
    model_choice_registry: ModelChoiceRegistry,
    mcp_registry: McpConnectionRegistry | None = None,
    persistence: Any | None = None,
    app_config: Any | None = None,
    kb_provider: KbProvider | None = None,
    strategy_registry: ExecutionStrategyRegistry | None = None,
) -> PoolInstance:
    """Build one PoolInstance's DEPLOYMENT resources from PoolSpec + deps.

    Strategy-agnostic (ADR-0025, ticket 6): resolves the strategy from
    ``pool_spec.main.execution_strategy``, calls ``strategy.assemble(ctx)``,
    and runs common post-assembly wiring. ``ProviderUnavailableError`` is
    caught to skip main-agent registration, leaving the pool structurally
    intact for peer routing.
    """
    main_spec = pool_spec.main
    main_agent_name = main_spec.agent_name
    system_prompt = resolve_system_prompt(main_agent_name, project_dir)

    inbox_dir = data_dir / "inbox" / pool_name
    inbox_db_path = data_dir / "state.db"
    inbox_server = build_inbox(
        app_config,
        persistence,
        inbox_dir,
        inbox_db_path,
        pool_name,
    )
    inbox_producer = InboxProducer(server=inbox_server)
    inbox_consumer = InboxConsumer(server=inbox_server)
    agent_bus = LocalAgentMessageBus(producer=inbox_producer, consumer=inbox_consumer)

    registry = strategy_registry
    if registry is None:
        registry = _default_strategy_registry()
    strategy_name = (
        main_spec.execution_strategy.value
        if main_spec.execution_strategy
        in (ExecutionStrategyKind.REACT, ExecutionStrategyKind.EXTERNAL)
        else "react"
    )
    strategy = registry.resolve(strategy_name)
    strategy.validate_pool_spec(pool_spec)

    ctx = _build_assembly_context(
        pool_name=pool_name,
        pool_spec=pool_spec,
        project_dir=project_dir,
        data_dir=data_dir,
        broker=broker,
        inbox_server=inbox_server,
        agent_bus=agent_bus,
        output_adapter=output_adapter,
        safety=safety,
        retention=retention,
        workspace_handle=workspace_handle,
        workspace_resolver=workspace_resolver,
        emitter_factory=emitter_factory,
        app_config=app_config,
        persistence=persistence,
        mcp_registry=mcp_registry,
        shared_hooks=shared_hooks,
        shared_hook_runner=shared_hook_runner,
        shared_interceptor_chain=shared_interceptor_chain,
        session_registry=session_registry,
        session_store=session_store,
        bot_model_config=bot_model_config,
        model_choice_registry=model_choice_registry,
        command_processor=command_processor,
        control_channel=control_channel,
        pool_data=pool_data,
        transcript_store=transcript_store,
        kb_provider=kb_provider,
        assembly_deps=assembly_deps,
    )

    if pool_data is not None:
        await ensure_long_term_defaults(
            project_dir, assembly_deps.memory, pool_data.context_manager.memory_system
        )

    provider_available = True
    external_deps: dict[str, Any] | None = None
    try:
        assembly = await strategy.assemble(ctx)
    except ProviderUnavailableError as exc:
        logger.warning(
            "Pool '%s': external provider %r not found on PATH; skipping pool registration",
            pool_name,
            exc.executable,
        )
        provider_available = False
        assembly = None

    if assembly is not None:
        provider = assembly.provider
        terminal_manager = assembly.terminal_manager
        tool_manager = assembly.tool_manager
        mcp_manager = assembly.mcp_manager
        todo_store = assembly.todo_store
        skill_manager = assembly.skill_manager
        context_manager = assembly.context_manager
        cassette_recorder = assembly.cassette_recorder
        root_provider = assembly.root_provider
        if assembly.external_deps is not None:
            external_deps = assembly.external_deps
    else:
        provider = None
        terminal_manager = None
        tool_manager = None
        mcp_manager = None
        todo_store = None
        skill_manager = None
        context_manager = None
        cassette_recorder = None
        root_provider = None

    default_resolved = _resolved_or_placeholder(bot_model_config).default_resolved()

    from modex_agent.multi_agent.session_tree.session_binding import (
        InMemorySessionBindingStore,
    )

    session_binding_store = InMemorySessionBindingStore()

    # Wrap before _build_agent_factory AND AgentMaterializeDeps so both the
    # main-agent _create_with_emitter path and the external-subagent
    # BotSubagentExternalBuilder path receive the same wrapped factory.
    if emitter_factory is not None and workspace_resolver is not None:
        emitter_factory = _WorkspaceEmitterFactory(
            emitter_factory,
            lambda: _cell_sessions_dir(workspace_resolver),
        )

    factory = _build_agent_factory(
        provider,
        tool_manager,
        skill_manager,
        inbox_server,
        inbox_consumer,
        shared_hooks,
        shared_hook_runner,
        shared_interceptor_chain,
        control_channel,
        workspace_resolver,
        pool_name,
        emitter_factory,
        external_deps=external_deps,
        observability_config=app_config.observability if app_config is not None else None,
        session_registry=session_registry,
        session_binding_store=session_binding_store,
    )
    session_factory = SessionIdFactory()

    if context_manager is None:
        context_manager = _fallback_context_manager(main_spec, system_prompt)
    pool = _build_agent_pool(
        broker,
        factory,
        agent_bus,
        inbox_consumer,
        session_factory,
        safety,
        retention,
        pool_name,
        session_registry=session_registry,
        session_store=session_store,
    )

    template_registry = AgentTemplateRegistry(
        PoolStore(base_dir=project_dir),
        default_subagent_memory=subagent_memory(),
    )
    templates = template_registry.list_templates(pool_name)
    logger.info("Pool '%s': %d subagent templates available", pool_name, len(templates))
    fallback_runtime_dir = data_dir / "runtime_state" / pool_name
    fallback_runtime_dir.mkdir(parents=True, exist_ok=True)
    path_resolver = WorkspacePathResolver(
        workspace_manager=workspace_resolver,
        pool_name=pool_name,
        fallback_runtime_dir=fallback_runtime_dir,
    )
    context_fork_builder = ContextForkBuilder()

    notification_service = AgentNotificationService(
        output_adapter=output_adapter,
    )

    if tool_manager is None:
        from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig

        tool_manager = InMemoryToolManager(config=ToolManagerConfig())

    # ADR-0027 T8: inject a SubagentExternalBuilder iff this pool
    # declares at least one external subagent. React-only pools
    # leave it None — AgentTemplate._materialize_external raises only when
    # an EXTERNAL subagent is dispatched without a builder.
    subagent_external_builder = _maybe_build_external_subagent_builder(
        pool_spec=pool_spec,
        pool_name=pool_name,
        project_dir=project_dir,
        data_dir=data_dir,
        app_config=app_config,
        persistence=persistence,
    )

    subagent_store_registry = None
    if pool_data is not None:
        ms = pool_data.context_manager.memory_system
        if ms is not None:
            subagent_store_registry = ms.store_registry

    control_origin = build_control_origin(project_dir / "config")

    # Poller + tree_manager are constructed BEFORE deps because deps.tree
    # is mandatory (todo 16). Consumer is callback-less at construction;
    # set_on_consumed binds the callback after tree_manager exists (todo 17),
    # breaking the cycle: consumer → bus → pool/poller → tree_manager →
    # consumer.set_on_consumed.
    poller = InboxPoller(pool, interval=0.2)
    pool.attach_poller(poller)
    agent_bus.set_poller(poller)

    tree_store, node_store, track_store = build_session_tree_stores(
        app_config,
        persistence,
        data_dir / "session_tree" / pool_name,
        BotRecordScope(pool=pool_name),
    )
    tree_manager = SessionTreeManager(
        tree_store=tree_store,
        node_store=node_store,
        track_store=track_store,
        bus=agent_bus,
        poller=poller,
        pool_name=pool_name,
        workspace_root=str(project_dir),
        session_registry=session_registry or InMemorySessionRegistry(),
        binding_store=session_binding_store,
    )
    inbox_consumer.set_on_consumed(tree_manager.on_consumed)
    poller.attach_tree_manager(tree_manager)

    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=session_factory,
        broker=broker,
        tree=tree_manager,
        safety=safety,
        llm_model=default_resolved.model.model,
        llm_temperature=default_resolved.model.temperature,
        llm_max_output_tokens=default_resolved.model.max_output_tokens,
        llm_reasoning_effort=default_resolved.model.reasoning_effort,
        llm_model_info=ModelInfo(
            model_name=default_resolved.model.model,
            capabilities=default_resolved.capabilities,
        ),
        project_dir=project_dir,
        notification_service=notification_service,
        inbox_consumer=inbox_consumer,
        agent_bus=agent_bus,
        output_adapter_factory=output_adapter_factory,
        root_provider=root_provider,
        session_registry=session_registry,
        on_subagent_created=on_subagent_created,
        context_fork_builder=context_fork_builder,
        workspace_path_resolver=path_resolver,
        mcp_registry=mcp_registry,
        todo_store=todo_store,
        trace_enabled=_resolve_trace_enabled(app_config),
        subagent_external_builder=subagent_external_builder,
        emitter_factory=emitter_factory,
        control_origin=control_origin,
        memory_store_registry=subagent_store_registry,
    )
    pool.materialize_deps = deps
    pool.template_registry = template_registry
    pool.pool_name = pool_name
    pool.context_fork_builder = context_fork_builder

    if provider_available:
        await _register_main_agent(
            pool,
            main_spec,
            assembly_deps,
            system_prompt,
            safety,
            pool_name,
            factory=factory,
            broker=broker,
            context_manager=context_manager,
            bot_model_config=bot_model_config,
            output_adapter=output_adapter,
        )
    else:
        logger.warning("Pool '%s': main agent registration skipped", pool_name)

    # Recover stale session-tree state BEFORE starting the poller — recovery
    # of stale terminal nodes and pending-input rebuilds must complete before
    # dispatch begins (todo 19).
    for record in await tree_store.list_active():
        await tree_manager.recover_tree(record.tree_id)

    # Start the poller AFTER main agent registration to eliminate the startup
    # race where pending messages from a previous run are dispatched before
    # the main agent is ready — causing "no template for X; skipping".
    pool.start_poller()

    # NOTE: ``pool_data`` is non-None for ALL pools — including external
    # (Pi/OpenCode) pools whose ``build_pool_data`` still builds a
    # ``DefaultMemorySystem``.  For external pools the hooks are registered
    # but never fire: ``ExternalTurnRunner`` uses an empty
    # ``ListMessageHistory`` and never appends to the framework memory
    # system, so ``cleanup_session`` is never invoked.  The registration is
    # intentionally kept (rather than guarded on strategy) so that if
    # external agents ever gain native memory-system support the notices
    # work without rewiring.
    if pool_data is not None:
        memory_system = pool_data.context_manager.memory_system
        if memory_system is not None:
            memory_system.add_cleanup_hook(UserNoticeCleanupHook(notification_service))
            memory_cfg = assembly_deps.memory
            has_archive = (
                memory_cfg is not None
                and memory_cfg.archive is not None
                and memory_cfg.archive.enabled
            )
            memory_system.add_cleanup_hook(
                TodoReorientationHook(todo_store, has_archive=has_archive)
            )

    main_service, main_store = _build_communication(
        pool,
        main_agent_name,
        project_dir,
        pool_name,
        templates,
        template_registry,
        tree=tree_manager,
        session_registry=session_registry,
        workspace_path_resolver=path_resolver,
        trace_enabled=_resolve_trace_enabled(app_config),
    )
    main_service._target_store = main_store

    if strategy.requires_main_agent_tools:
        if main_store.list():
            tool_manager.register(
                TaskDispatchTool(
                    store=main_store,
                    source=AgentAddress(name=main_agent_name),
                    service=main_service,
                )
            )
            logger.info("Pool '%s': task tool registered (subagent dispatch + peer communication)", pool_name)
        else:
            logger.info("Pool '%s': no communication targets — task tool not registered", pool_name)

        _wire_main_pipeline(
            pool,
            main_agent_name,
            inbox_consumer,
            notification_service,
            shared_interceptor_chain,
            im_ui,
            main_spec,
            assembly_deps,
            project_dir,
            command_processor,
            pool_name,
            tool_manager=tool_manager,
            pool_spec=pool_spec,
            root_provider=root_provider,
            bot_model_config=bot_model_config,
            model_choice_registry=model_choice_registry,
            cassette_recorder=cassette_recorder,
            control_origin=control_origin,
            graph_context_resolver=(
                _make_graph_context_resolver(workspace_resolver)
                if workspace_resolver is not None
                else None
            ),
            session_binding_store=session_binding_store,
        )
    else:
        # external path: the external agent has no tool surface and
        # communicates via ``modexctl send`` CLI (not the ``task`` tool), so
        # skip task tool registration and the react-only
        # ``_wire_main_pipeline`` (governance/approval/hooks). Only set
        # ``command_processor`` on the pipeline so pre-lock ``/stop``
        # dispatch still works.
        if command_processor is not None:
            main_instance = pool._agents.get(main_agent_name)
            if main_instance is not None and main_instance.pipeline is not None:
                main_instance.pipeline.command_processor = command_processor
        logger.info(
            "Pool '%s': external — skipped task tool + _wire_main_pipeline",
            pool_name,
        )

    bridge = BrokerBridgeService(
        broker=broker,
        input_bindings={},
        output_routes=[
            OutputRoute(adapter=output_adapter, match_topic=f"agent:{main_agent_name}:out"),
        ],
    )

    return PoolInstance(
        name=pool_name,
        media=assembly_deps.media,
        subagent_count=len(pool_spec.subagents),
        pool=pool,
        broker_bridge=bridge,
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        terminal_manager=terminal_manager,
        main_agent_name=main_agent_name,
        main_execution_strategy=pool_spec.main.execution_strategy,
        provider=provider,
        notification_service=notification_service,
        communication_service=main_service,
        tree_manager=tree_manager,
        target_store=main_store,
        session_binding_store=session_binding_store,
    )
