"""Pool factory - IOC-style factory that builds one PoolInstance from a
declared pool.

Extracted from ``pool_builder.py`` (ADR-0025 ticket 6 split). Each build step
is a focused method.  Convention over configuration: config drives behaviour;
methods read from the scope declaration (``PoolSpec`` / the root ``AgentSpec``)
with sensible defaults.  No giant if-else chains.

The strategy-specific ``_build_*`` helpers
(``_build_tools``,
``_build_skill_manager``, ``_resolve_cassette_config``,
``_fallback_context_manager``, ``_cell_sessions_dir``) live on the shared
:class:`bot.service.builders._PoolAssemblyMixin`, inherited by both
:class:`ReactExecutionStrategy` and :class:`ExternalExecutionStrategy`.
LLM providers are NOT strategy helpers: both production entries (main at
``create_pool``, sub at the deps assembly) resolve the LLM slot once via
``_resolve_llm_slot`` and pass the resolved instance down.
``create_pool`` is now strategy-agnostic: it resolves the strategy, calls
``strategy.assemble(ctx)``, and runs the common post-assembly wiring
(register main agent, communication, pipeline construction via factory).
Both react and external pools follow the same code path here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from bot.config.webui_config import build_control_origin
from bot.scope import BotRecordScope
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.session_pool_index import SessionPoolIndex
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.agent import AgentCommKind
from modex_agent.core.capabilities import ModelInfo
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.hook import Hook, HookRunner
from modex_agent.hook.notification import AgentNotificationService
from modex_agent.ioc.factories.session_tree import build_session_tree_stores
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.messaging.broker_bridge import BrokerBridgeService, OutputRoute
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
)
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    CommunicationFacilities,
    PoolRuntimeDeps,
    SupplyInfra,
    resolution_context,
)
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    _resolve_single,
)
from modex_agent.plugins.assembly.pipeline import AssemblyPipeline
from modex_agent.plugins.assembly.stages.agent_assemble import AgentAssembleStage
from modex_agent.plugins.assembly.stages.infra_assemble import InfraAssembleStage
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.assembly.stages.workspace_materialize import (
    WorkspaceMaterializeStage,
)
from modex_agent.plugins.registry import (
    ComponentRegistry,
    strategy_registry_from_components,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath

from ..builders import build_inbox, resolve_declared_root_prompt
from ..external_strategy import ProviderUnavailableError
from ..model_config import _resolved_or_placeholder
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
from .declaration import DeclaredPoolBuild
from .pipeline_wiring import _wire_main_pipeline
from .pool_construction import (
    _build_agent_pool,
    _register_external_main_agent,
    ensure_long_term_defaults,
)

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
    from modex_agent.core.provider import LLMProvider
    from modex_agent.media.store import MediaStore
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
        PoolAssemblyContext,
    )
    from modex_agent.plugins.assembly.builder import AssembledAgent, AssemblyBuilder
    from modex_agent.plugins.assembly.spec import AssemblySpec
    from modex_agent.scope.spec import WorkspaceSpec
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_graph.context import GraphContext

logger = logging.getLogger(__name__)

_BOT_DEFAULT_LLM_PROVIDER: Final = "bot_default"


async def _resolve_llm_slot(
    registry: ComponentRegistry,
    name: str,
    config: Mapping[str, object],
    pool_assembly_ctx: PoolAssemblyContext,
    workspace_ctx: WorkspaceContext,
) -> LLMProvider:
    """Resolve an LLM_PROVIDER slot name to an instance (C1 single path).

    ``pool_assembly_ctx`` is the registry-injected context (bot_model_config
    already placeholder-wrapped, model_choice_registry threaded) — the same
    context the ``bot_default`` factory validates against.
    """
    component_ctx = resolution_context(
        registry,
        workspace_ctx,
        PoolRuntimeDeps(pool_assembly_ctx=pool_assembly_ctx),
    )
    return await _resolve_single(
        registry,
        ComponentSlot.LLM_PROVIDER,
        name,
        config,
        component_ctx,
    )


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
    declared: DeclaredPoolBuild,
    assembly_deps: PoolAssemblyDeps,
    *,
    project_dir: Path,
    data_dir: Path,
    broker: Any,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    im_ui: Any,
    shared_hooks: list[Hook],
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    control_channel: InMemoryControlChannel | None = None,
    command_processor: Any = None,
    pool_data: PoolDataSnapshot | None = None,
    workspace_handle: WorkspaceHandle | None = None,
    workspace_resolver: WorkspaceResolverCell | None = None,
    media_store_resolver: Callable[[], MediaStore] | None = None,
    # Business factory: (session_id, pool); pool-bound below for the framework.
    emitter_factory: Callable[[str, str], ContentEmitter[Any]] | None = None,
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    # Business callback: (child_id, parent_id, pool); pool-bound below.
    on_subagent_created: Callable[[str, str, str], Awaitable[None]] | None = None,
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
    # Per-workspace session→pool attribution index; the tree/node stores built
    # below register into it.
    session_pool_index: SessionPoolIndex | None = None,
    # Supply-mode: the caller (resources.py factory body) pre-fills
    # workspace_registry + workspace_resources to prevent the recursive
    # single-flight deadlock (pipeline.run → WorkspaceMaterializeStage →
    # registry.materialize → re-enter factory body). Required — the
    # pipeline is the single assembly path (no legacy branch).
    workspace_registry: Any,
    workspace_resources: Any,
    # Service-level ComponentRegistry singleton: threaded from BotService so
    # every pool resolves against the same factory set. None → local registry
    # (DefaultPlugin + BotStrategiesPlugin) for framework-style tests.
    component_registry: ComponentRegistry | None = None,
    # The declared workspace resource selection (ticket 14, SPEC §3.1) —
    # carried onto the assembly context chain's workspace layer so
    # factories can read the workspace's declared backend/paths/MCP set.
    # None for pool-as-root and no-declaration boots.
    workspace_spec: WorkspaceSpec | None = None,
) -> PoolInstance:
    """Build one PoolInstance's DEPLOYMENT resources from the declared pool.

    Strategy-agnostic (ADR-0025, ticket 6): resolves the strategy from the
    declared root's ``execution_strategy``, calls ``strategy.assemble(ctx)``,
    and runs common post-assembly wiring. ``ProviderUnavailableError`` is
    caught to skip main-agent registration, leaving the pool structurally
    intact for peer routing.
    """
    pool_spec = declared.pool
    main_spec = pool_spec.root_agent
    root_agent_name = main_spec.name

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

    if component_registry is not None:
        resolved_registry = component_registry
    else:
        from modex_agent.plugins.defaults import DefaultPlugin
        from modex_agent.plugins.loader import (
            ComponentRegistryLoader,
            PluginDiscoveryConfig,
        )

        # Same discovery shape as the production entry (core.py), anchored
        # to THIS module's project root — never a ``plugins`` package
        # import (any ``plugins`` package already on sys.path, e.g. a test
        # directory, would shadow it).
        bot_project_dir = Path(__file__).resolve().parents[3]
        resolved_registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            resolved_registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(bot_project_dir / "plugins",),
            ),
        )

    system_prompt = await resolve_declared_root_prompt(
        declared,
        project_dir,
        resolved_registry,
    )

    registry = strategy_registry or strategy_registry_from_components(resolved_registry)
    strategy_name = strategy_name_of(main_spec.execution_strategy)
    strategy = registry.resolve(strategy_name)
    strategy.validate_pool_spec(pool_spec)

    _pool_bound_emitter: Callable[[str], ContentEmitter[Any]] | None = None
    if emitter_factory is not None:

        def pool_bound_emitter(session_id: str) -> ContentEmitter[Any]:
            return emitter_factory(session_id, pool_name)

        _pool_bound_emitter = pool_bound_emitter

    _pool_bound_on_created: Callable[[str, str], Awaitable[None]] | None = None
    if on_subagent_created is not None:

        async def pool_bound_on_created(child_id: str, parent_id: str) -> None:
            await on_subagent_created(child_id, parent_id, pool_name)

        _pool_bound_on_created = pool_bound_on_created

    ctx = _build_assembly_context(
        pool_name=pool_name,
        pool_spec=pool_spec,
        peer_links=declared.peer_links,
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
        emitter_factory=_pool_bound_emitter,
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
        memory_system = pool_data.context_manager.memory_system
        if memory_system is not None:
            await ensure_long_term_defaults(project_dir, assembly_deps.memory, memory_system)

    session_factory = SessionIdFactory()
    pool = _build_agent_pool(
        broker,
        None,
        agent_bus,
        inbox_consumer,
        session_factory,
        safety,
        retention,
        pool_name,
        session_registry=session_registry,
        session_store=session_store,
    )

    workspace_ctx_for_spec = WorkspaceContext(
        target=project_dir,
        paths=WorkspacePaths(root=data_dir),
        is_home=False,
    )
    # The ScopeCompiler's product IS the main assembly spec (ticket 07) —
    # no roster projection, no re-derivation.
    main_assembly_spec = declared.root.spec
    # Roster hook names dispatched onto the main agent by Stage 4 assembly;
    # code-wired default sites skip these (roster wins — D-A8).
    roster_hook_names = frozenset(main_assembly_spec.hooks)
    ctx = replace(
        ctx,
        bot_model_config=_resolved_or_placeholder(bot_model_config),
        component_registry=resolved_registry,
        assembly_spec=main_assembly_spec,
    )

    from modex_agent.multi_agent.session_tree.session_binding import (
        InMemorySessionBindingStore,
    )

    session_binding_store = InMemorySessionBindingStore()
    default_resolved = _resolved_or_placeholder(bot_model_config).default_resolved()

    # C1 sub path: the subagent-default provider name resolves once here
    # (hoisted before the pipeline, ticket 09): it is the SUPPLIED
    # bot-global default provider for the ``experience_review`` HOOK-slot
    # factory (the reviewer must not depend on any pool's own provider).
    # External pools keep None.
    sub_default_llm_provider: LLMProvider | None = None
    if strategy.requires_llm_provider:
        sub_default_llm_provider = await _resolve_llm_slot(
            resolved_registry,
            _BOT_DEFAULT_LLM_PROVIDER,
            {},
            ctx,
            workspace_ctx_for_spec,
        )

    # Template registry + path resolver + poller/tree_manager are
    # constructed BEFORE the assembly pipeline: the derived communication
    # TOOL factories resolve at Stage 4 against pool-layer facilities that
    # need the tree manager and the (declaration-seeded or disk-scanned)
    # template registry — none of which depend on the pipeline's products.
    # The poller itself stays inert until `pool.start_poller()` below.
    # Consumer is callback-less at construction; set_on_consumed binds the
    # callback after tree_manager exists (todo 17), breaking the cycle:
    # consumer → bus → pool/poller → tree_manager → consumer.set_on_consumed.
    template_registry = declared.template_registry
    templates = template_registry.list_templates(pool_name)
    logger.info("Pool '%s': %d subagent templates available", pool_name, len(templates))
    scope_path = ScopePath(
        workspace_root=(
            workspace_handle.current if workspace_handle is not None else project_dir
        ),
        pool_name=pool_name,
    )
    context_fork_builder = ContextForkBuilder()

    poller = InboxPoller(pool, interval=0.2)
    pool.attach_poller(poller)
    agent_bus.set_poller(poller)

    tree_store, node_store, track_store = build_session_tree_stores(
        app_config,
        persistence,
        data_dir / "session_tree" / pool_name,
        BotRecordScope(pool=pool_name),
    )
    # SessionTreeManager keeps its stores private; these local handles are the
    # only seam where the attribution index can capture them.
    if session_pool_index is not None:
        session_pool_index.register(pool_name, tree_store, node_store)
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

    # Declaration road (ticket 07): the root's per-agent communication
    # facilities — service + target store populated from the DERIVED child
    # entries (SPEC §5.2), replacing the legacy template-driven pool-level
    # store. Ticket 12: the store lists the root's DIRECT children only
    # (declared.root_children) — grandchildren are the mid-level agent's
    # own dispatch surface. Peer targets join the same store at workspace
    # materialize time through the FW resolution service (ticket 13).
    root_store = CommunicationTargetStore()
    for child in declared.root_children:
        root_store.add(
            CommunicationTarget(
                name=child.spec.agent_name,
                kind=AgentCommKind.SUBAGENT,
                description=child.spec.description,
                execution_strategy=ExecutionStrategyKind(
                    child.spec.execution_strategy
                ),
            )
        )
    communication_facilities = CommunicationFacilities(
        service=AgentCommunicationService(
            source=AgentAddress(name=root_agent_name),
            registry=pool,
            tree=tree_manager,
            template_registry=template_registry,
            pool=pool,
            pool_name=pool_name,
            project_dir=project_dir,
            session_registry=session_registry,
            scope_path=scope_path,
            workspace_manager=workspace_resolver,
            trace_enabled=_resolve_trace_enabled(app_config),
            target_store=root_store,
        ),
        target_store=root_store,
    )

    # C1 main path: the main agent's LLM_PROVIDER name resolves to an
    # instance exactly once here, feeding BOTH the agent factory and the
    # Stage-4 assembly inputs. External pools own their model config — None.
    main_llm_provider: LLMProvider | None = None
    if strategy.requires_llm_provider:
        main_llm_provider = await _resolve_llm_slot(
            resolved_registry,
            main_assembly_spec.llm_provider,
            main_assembly_spec.llm_provider_config,
            ctx,
            workspace_ctx_for_spec,
        )
    # What the main agent actually runs on (cassette-wrapped in Stage 4 when
    # recording); exported for PoolInstance.
    main_provider: LLMProvider | None = None

    factory = None

    _workspace_emitter_factory = _pool_bound_emitter
    if _pool_bound_emitter is not None and workspace_resolver is not None:
        _workspace_emitter_factory = _WorkspaceEmitterFactory(
            _pool_bound_emitter,
            lambda: _cell_sessions_dir(workspace_resolver),
        )

    def build_native_inputs(
        _spec: AssemblySpec,
        builder: AssemblyBuilder,
        _assembly_context: AssemblyContext,
    ) -> NativeAssemblyInputs:
        nonlocal factory, main_provider
        strategy_result = builder.strategy_result
        if strategy_result is None:
            raise RuntimeError("Native Stage4 requires the Stage3 strategy result")
        provider = main_llm_provider
        if strategy_result.cassette_recorder is not None:
            provider = strategy_result.cassette_recorder.wrap_provider(provider)
        main_provider = provider
        factory = _build_agent_factory(
            provider,
            strategy_result.tool_manager,
            strategy_result.skill_manager,
            inbox_server,
            inbox_consumer,
            shared_hooks,
            shared_hook_runner,
            shared_interceptor_chain,
            control_channel,
            workspace_resolver,
            pool_name,
            _workspace_emitter_factory,
            media_store_resolver=media_store_resolver,
            observability_config=(app_config.observability if app_config is not None else None),
            session_registry=session_registry,
            session_binding_store=session_binding_store,
            trace_store=pool_data.trace_store if pool_data is not None else None,
        )
        pool._agent_factory = factory
        return NativeAssemblyInputs(
            agent_factory=factory,
            broker=broker,
            llm_defaults=LlmDefaults(
                model=default_resolved.model.model,
                temperature=default_resolved.model.temperature,
                max_output_tokens=default_resolved.model.max_output_tokens,
                reasoning_effort=default_resolved.model.reasoning_effort,
                model_info=ModelInfo(
                    model_name=default_resolved.model.model,
                    capabilities=default_resolved.capabilities,
                ),
            ),
            pool=pool,
            context_manager=strategy_result.context_manager,
            memory_system=(
                pool_data.context_manager.memory_system if pool_data is not None else None
            ),
            memory_config=assembly_deps.memory,
            llm_provider=provider,
            tool_manager=strategy_result.tool_manager,
            skill_manager=strategy_result.skill_manager,
            output_adapter=output_adapter,
            root_provider=strategy_result.root_provider,
            safety=safety,
            project_dir=project_dir,
        )

    assembly_pipeline = AssemblyPipeline(
        workspace_materialize=WorkspaceMaterializeStage(),
        infra_assemble=InfraAssembleStage(),
        pool_assemble=PoolAssembleStage(),
        agent_assemble=AgentAssembleStage(build_native_inputs),
    )

    # Constructed BEFORE the pipeline (ticket 09): HOOK-slot factories
    # dispatched at Stage 4 (user_notice_cleanup, experience_review) resolve
    # their runtime deps from the chain — the notification service and the
    # bot-global default review provider ride SupplyInfra into
    # PoolRuntimeDeps.
    notification_service = AgentNotificationService(
        output_adapter=output_adapter,
    )
    experience_review_provider = sub_default_llm_provider
    infra = SupplyInfra(
        pool_assembly_ctx=ctx,
        pool=pool,
        communication=communication_facilities,
        notification_service=notification_service,
        experience_review_provider=experience_review_provider,
    )
    assembly_ctx = AssemblyContext(
        registry=resolved_registry,
        workspace_registry=workspace_registry,  # type: ignore[arg-type]
        workspace_ctx=workspace_ctx_for_spec,
        workspace_resources=workspace_resources,
        workspace_spec=workspace_spec,
        infra=infra,
    )

    provider_available = True
    external_deps: dict[str, Any] | None = None
    assembled: AssembledAgent | None = None
    try:
        assembled = await assembly_pipeline.run(main_assembly_spec, assembly_ctx)
        assembly = assembled.strategy_result
    except ProviderUnavailableError as exc:
        logger.warning(
            "Pool '%s': external provider %r not found on PATH; skipping pool registration",
            pool_name,
            exc.executable,
        )
        provider_available = False
        assembly = None

    if assembly is not None:
        terminal_manager = assembly.terminal_manager
        tool_manager = assembly.tool_manager
        todo_store = assembly.todo_store
        skill_manager = assembly.skill_manager
        context_manager = assembly.context_manager
        cassette_recorder = assembly.cassette_recorder
        root_provider = assembly.root_provider
        component_hook_specs = assembly.component_hook_specs
        if assembly.external_deps is not None:
            external_deps = assembly.external_deps
    else:
        terminal_manager = None
        tool_manager = None
        todo_store = None
        skill_manager = None
        context_manager = None
        cassette_recorder = None
        root_provider = None
        component_hook_specs = ()
    # Per-agent MCP loading happens at Stage 4 (ticket 10) — the live
    # backend rides the pipeline product, not the strategy result.
    mcp_manager = assembled.mcp_manager if assembled is not None else None

    # Pool binding must precede this workspace wrapper. Both the main-agent
    # _create_with_emitter path and the external-subagent materialization path
    # then receive the same pool-bound, workspace-aware factory.
    # External pools only (native pools built the factory in Stage 4): no
    # provider — the external CLI owns its model config.
    if factory is None:
        factory = _build_agent_factory(
            None,
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
            _workspace_emitter_factory,
            media_store_resolver=media_store_resolver,
            external_deps=external_deps,
            observability_config=(app_config.observability if app_config is not None else None),
            session_registry=session_registry,
            session_binding_store=session_binding_store,
            trace_store=pool_data.trace_store if pool_data is not None else None,
        )
    pool._agent_factory = factory  # type: ignore[attr-defined]

    if context_manager is None:
        context_manager = _fallback_context_manager(main_spec, system_prompt)

    if tool_manager is None:
        from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig

        tool_manager = InMemoryToolManager(config=ToolManagerConfig())

    subagent_store_registry = None
    if pool_data is not None:
        ms = pool_data.context_manager.memory_system
        if ms is not None:
            subagent_store_registry = ms.store_registry

    control_origin = build_control_origin(project_dir / "config")
    # The lazy graph-context closure shared by the main pipeline AND the
    # subagent materialization deps (ticket 12 — one resolver, both paths).
    graph_context_resolver = (
        _make_graph_context_resolver(workspace_resolver)
        if workspace_resolver is not None
        else None
    )

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
        llm_provider=sub_default_llm_provider,
        project_dir=project_dir,
        notification_service=notification_service,
        inbox_consumer=inbox_consumer,
        agent_bus=agent_bus,
        output_adapter_factory=output_adapter_factory,
        root_provider=root_provider,
        session_registry=session_registry,
        on_subagent_created=_pool_bound_on_created,
        context_fork_builder=context_fork_builder,
        scope_path=scope_path,
        workspace_manager=workspace_resolver,
        mcp_registry=mcp_registry,
        todo_store=todo_store,
        execution_strategy=strategy,
        strategy_registry=registry,
        data_dir=data_dir,
        app_config=app_config,
        persistence=persistence,
        emitter_factory=_workspace_emitter_factory,
        control_origin=control_origin,
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        memory_store_registry=subagent_store_registry,
        component_registry=resolved_registry,
        pool_assembly_ctx=ctx,
        graph_context_resolver=graph_context_resolver,
    )
    pool.materialize_deps = deps
    pool.template_registry = template_registry
    pool.pool_name = pool_name
    pool.context_fork_builder = context_fork_builder

    # Main-agent registration. Native mains registered inside the pipeline
    # (Stage 4); external-shape mains (strategy capability flag, not a
    # caller-side identity branch) register here through the strategy-aware
    # factory — before tree recovery + poller start, so no pending message
    # races an unregistered main.
    if not provider_available:
        logger.warning("Pool '%s': main agent registration skipped", pool_name)
    elif not strategy.requires_main_agent_tools:
        await _register_external_main_agent(
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
            # `user_notice_cleanup` is roster-referenced and dispatched onto
            # this memory system by Stage 4 (native mains only; external
            # mains have no Stage 4 — their memory system never fires
            # cleanup, so the omission is behavior-neutral).
            memory_cfg = assembly_deps.memory
            has_archive = (
                memory_cfg is not None
                and memory_cfg.archive is not None
                and memory_cfg.archive.enabled
            )
            memory_system.add_cleanup_hook(
                TodoReorientationHook(todo_store, has_archive=has_archive)
            )

    # The facilities built pre-pipeline ARE the main service + per-agent
    # store (tools resolved at Stage 4 against them).
    main_service = communication_facilities.service
    main_store = communication_facilities.target_store or CommunicationTargetStore()
    logger.info(
        "Pool '%s': communication store (%d targets)",
        pool_name,
        len(main_store.list()),
    )
    main_service._target_store = main_store

    # Pool-level extensions (ticket 10): PoolAssembleStage resolves the
    # spec's INTERCEPTOR / COMMAND_HANDLER rosters against the
    # pool_runtime-enriched context; the BIZ resolution branch is deleted.
    # Fallbacks keep the legacy semantics: no roster interceptors → the
    # workspace-shared chain; no roster commands → the passed-in processor
    # or the default (only reachable when the pipeline never completed —
    # the external provider-unavailable shape).
    extensions_pool_runtime = (
        assembled.propagated_context.pool_runtime
        if assembled is not None and assembled.propagated_context is not None
        else None
    )
    pool_interceptor_chain = (
        extensions_pool_runtime.interceptor_chain
        if extensions_pool_runtime is not None
        else None
    ) or shared_interceptor_chain
    pool_command_processor = (
        extensions_pool_runtime.command_processor
        if extensions_pool_runtime is not None
        else None
    ) or command_processor or SlashCommandProcessor.default()

    if strategy.requires_main_agent_tools:
        _wire_main_pipeline(
            pool,
            root_agent_name,
            inbox_consumer,
            notification_service,
            pool_interceptor_chain,
            im_ui,
            main_spec,
            assembly_deps,
            project_dir,
            pool_command_processor,
            pool_name,
            tool_manager=tool_manager,
            pool_spec=pool_spec,
            peer_links=declared.peer_links,
            root_provider=root_provider,
            bot_model_config=bot_model_config,
            model_choice_registry=model_choice_registry,
            roster_hook_names=roster_hook_names,
            cassette_recorder=cassette_recorder,
            control_origin=control_origin,
            graph_context_resolver=graph_context_resolver,
            session_binding_store=session_binding_store,
            tree_manager=tree_manager,
            component_hook_specs=component_hook_specs,
            todo_store=todo_store,
        )
    else:
        # external path: the external agent has no tool surface (it
        # communicates via ``modexctl send`` CLI, so the derived
        # communication entries are absent from its compiled spec) and no
        # react-only ``_wire_main_pipeline`` (governance/approval/hooks).
        # Only set ``command_processor`` on the pipeline so pre-lock ``/stop``
        # dispatch still works.
        if pool_command_processor is not None:
            main_instance = pool._agents.get(root_agent_name)
            if main_instance is not None and main_instance.pipeline is not None:
                main_instance.pipeline.command_processor = pool_command_processor
        logger.info(
            "Pool '%s': external — skipped _wire_main_pipeline",
            pool_name,
        )

    bridge = BrokerBridgeService(
        broker=broker,
        input_bindings={},
        output_routes=[
            OutputRoute(adapter=output_adapter, match_topic=f"agent:{root_agent_name}:out"),
        ],
    )

    return PoolInstance(
        name=pool_name,
        media=assembly_deps.media,
        subagent_count=len(declared.subagents),
        pool=pool,
        broker_bridge=bridge,
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        terminal_manager=terminal_manager,
        root_agent_name=root_agent_name,
        main_execution_strategy=ExecutionStrategyKind(main_spec.execution_strategy),
        provider=main_provider,
        notification_service=notification_service,
        communication_service=main_service,
        tree_manager=tree_manager,
        target_store=main_store,
        session_binding_store=session_binding_store,
        requires_main_agent_tools=strategy.requires_main_agent_tools,
        roster_hook_names=roster_hook_names,
        comm_tools_derived=True,
    )
