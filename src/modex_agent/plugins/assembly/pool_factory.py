"""Pool factory — IOC-style factory that builds one PoolInstance from a
declared pool.

Promoted from ``examples/bot_project/bot/service/pool/factory.py`` +
``agent_factory.py`` + ``assembly_context.py`` + ``pool_construction.py``
(W4a, plan SD-7). Each build step is a focused helper. Convention over
configuration: config drives behaviour; helpers read from the scope
declaration (``PoolSpec`` / the root ``AgentSpec``) with sensible defaults.
No giant if-else chains.

The strategy-specific build helpers live on the bundled strategies
(:mod:`modex_agent.plugins.assembly.strategies`). LLM providers are NOT
strategy helpers: both production entries (main at ``create_pool``, sub at
the deps assembly) resolve the LLM slot once via ``_resolve_llm_slot`` and
pass the resolved instance down. ``create_pool`` is strategy-agnostic: it
resolves the strategy, calls ``strategy.assemble_main(ctx)``, and runs the
common post-assembly wiring (register main agent, communication, pipeline
construction via factory). Both react and external pools follow the same
code path here.

The deployment model universe is threaded through the injected
:class:`~modex_agent.plugins.assembly.model_assembly.PoolModelAssembly` seam
(since W4b the framework ships :class:`~modex_agent.app.models.assembly.ModelRegistryAssembly`);
``create_pool`` threads it onto ``PoolAssemblyContext.model_assembly`` together
with the typed ``bot_model_config`` / ``model_choice_registry`` fields for
deployment LLM factories.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.adapters.output import OutputAdapter
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.control.channel import ControlChannel
from modex_agent.core.emitter import TurnBinding, TurnEventSink, TurnEventSinkFactory
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.hook import Hook, HookRunner
from modex_agent.hook.notification import AgentNotificationService
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.communication.peer_resolution import PeerLink
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.execution_strategy import strategy_registry_from_components
from modex_agent.multi_agent.factory import AgentFactory, DefaultAgentFactory
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.inbox_poller import InboxPoller
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps
from modex_agent.multi_agent.pool_config.declared import DeclaredPoolBuild
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.pool_index import SessionPoolIndex
from modex_agent.multi_agent.session_tree.session_binding import (
    InMemorySessionBindingStore,
)
from modex_agent.multi_agent.tools import CommunicationTargetStore
from modex_agent.persistence.session_registry import InMemorySessionRegistry, SessionRegistry
from modex_agent.persistence.session_store import SessionStore
from modex_agent.pipeline.broker_bridge import BrokerBridgeService, OutputRoute
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.plugins.assembly.backend_factory import (
    build_approval_audit_store,
    build_inbox,
)
from modex_agent.plugins.assembly.context import (
    AgentContext as ComponentAgentContext,
)
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    PoolRuntimeDeps,
    SupplyInfra,
    agent_context_chain,
    resolution_context,
)
from modex_agent.plugins.assembly.model_assembly import PoolModelAssembly
from modex_agent.plugins.assembly.native_core import (
    NativeAssemblyInputs,
    resolve_single,
)
from modex_agent.plugins.assembly.pipeline import AssemblyPipeline
from modex_agent.plugins.assembly.pipeline_wiring import wire_main_pipeline
from modex_agent.plugins.assembly.session_tree_factory import build_session_tree_stores
from modex_agent.plugins.assembly.stages.agent_assemble import AgentAssembleStage
from modex_agent.plugins.assembly.stages.infra_assemble import InfraAssembleStage
from modex_agent.plugins.assembly.stages.pool_assemble import PoolAssembleStage
from modex_agent.plugins.assembly.stages.workspace_materialize import (
    WorkspaceMaterializeStage,
)
from modex_agent.plugins.assembly.strategies.external import (
    ProviderUnavailableError,
)
from modex_agent.plugins.assembly.subagent_materializer import (
    SubagentMaterializer,
    inject_emitter_and_pool_context,
)
from modex_agent.plugins.defaults.capabilities.skills import (
    SKILLS_CAPABILITY_NAME,
    require_skills_supply,
)
from modex_agent.plugins.defaults.capabilities.subagents import (
    SubagentsSupply,
    build_pool_communication_service,
)
from modex_agent.scope.component_registry import (
    ComponentRegistry,
)
from modex_agent.scope.components import ComponentSlot
from modex_agent.scope.execution_kind import strategy_name_of
from modex_agent.scope.spec import PoolSpec
from modex_agent.trace.observability import TraceBackend
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.scope_path import ScopePath

if TYPE_CHECKING:
    from modex_agent.app.config import AppConfig
    from modex_agent.app.models.choice import ModelChoiceRegistry
    from modex_agent.app.models.registry import ModelRegistry
    from modex_agent.commands.skill import SkillResolver
    from modex_agent.core.media import MediaStore
    from modex_agent.core.prompt import SystemPromptProvider
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.core.workspace_root import WorkspaceRootProvider
    from modex_agent.hook.abc import HookSpec
    from modex_agent.memory.context import ContextManager
    from modex_agent.messaging.broker import MessageBroker
    from modex_agent.multi_agent import AgentPool
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
        MainAssembly,
        PoolAssemblyContext,
    )
    from modex_agent.multi_agent.session_tree.session_binding import SessionBindingStore
    from modex_agent.plugins.assembly.builder import AssembledAgent, AssemblyBuilder
    from modex_agent.plugins.defaults.capabilities.approval.ui import (
        ApprovalUserInterface,
    )
    from modex_agent.scope.assembly_spec import AssemblySpec
    from modex_agent.scope.spec import WorkspaceSpec
    from modex_agent.tools.mcp.registry import McpConnectionRegistry
    from modex_agent.trace.cassette import CassetteRecorder
    from modex_agent.workspace.handle import WorkspaceHandle
    from modex_agent.workspace.resources import WorkspaceManager
    from modex_graph.context import GraphContext

logger = logging.getLogger(__name__)

__all__ = [
    "create_pool",
    "resolve_declared_root_prompt",
]


# ═══════════════════════════════════════════════════════════════════════════
# LLM slot resolution (C1 single path)
# ═══════════════════════════════════════════════════════════════════════════


async def _resolve_llm_slot(
    registry: ComponentRegistry,
    name: str,
    config: Mapping[str, object],
    pool_assembly_ctx: PoolAssemblyContext,
    workspace_ctx: WorkspaceContext,
) -> LLMProvider:
    """Resolve an LLM_PROVIDER slot name to an instance (C1 single path).

    ``pool_assembly_ctx`` is the registry-injected context (the resolved
    model config already threaded, model_choice_registry carried) — the
    same context the deployment default factory validates against.
    """
    component_ctx = resolution_context(
        registry,
        workspace_ctx,
        PoolRuntimeDeps(pool_assembly_ctx=pool_assembly_ctx),
    )
    spec = pool_assembly_ctx.assembly_spec
    if spec is None:
        raise ValueError("LLM slot resolution requires the pool's root assembly spec")
    return await resolve_single(
        registry,
        ComponentSlot.LLM_PROVIDER,
        name,
        config,
        agent_context_chain(component_ctx, spec=spec),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Declared root prompt resolution
# ═══════════════════════════════════════════════════════════════════════════


async def resolve_declared_root_prompt(
    declared: DeclaredPoolBuild,
    project_dir: Path,
    registry: ComponentRegistry,
) -> str:
    """Resolve the compiled root's declared system-prompt provider."""
    spec = declared.root.spec
    prompt_config = dict(spec.system_prompt_config)
    if "path" in prompt_config:
        prompt_path = Path(prompt_config["path"])
        if not prompt_path.is_absolute():
            prompt_config["path"] = str(project_dir / prompt_path)
    workspace = WorkspaceContext(
        target=project_dir,
        paths=WorkspacePaths(root=project_dir / ".modex"),
        is_home=False,
    )
    factory = registry.resolve(ComponentSlot.SYSTEM_PROMPT_PROVIDER, spec.system_prompt_provider)
    config = factory.config_model.model_validate(prompt_config)
    provider: SystemPromptProvider = await factory.create(
        config,
        ComponentAgentContext(
            registry=registry,
            workspace_ctx=workspace,
            agent_name=spec.agent_name,
            spec=spec,
        ),
    )
    return await provider.get_or_refresh()


# ═══════════════════════════════════════════════════════════════════════════
# Graph-context resolver (lazy closure)
# ═══════════════════════════════════════════════════════════════════════════


def _make_graph_context_resolver(
    workspace_resolver: WorkspaceManager,
) -> Callable[[int], GraphContext[Any] | None]:
    """Build a lazy graph-context resolver closure for the main pipeline.

    Defers workspace resolution + ``graph_orchestrator`` dereference to
    invocation time so the closure stays robust against late workspace
    materialization (the cell is filled after pool creation) and
    orchestrator LRU eviction (F6-verified pattern).

    ``graph_orchestrator`` is a DEPLOYMENT extension of the workspace
    resource bundle (the framework ``WorkspaceResources`` contract carries
    only pool data + identity) — the getattr-with-default is the extension
    boundary; bundles without a graph subsystem yield ``None``.
    """

    def resolve(gid: int) -> GraphContext[Any] | None:
        resources = workspace_resolver.resolve_workspace()
        orchestrator = getattr(resources, "graph_orchestrator", None)
        if orchestrator is None:
            return None
        return orchestrator.get_graph_context(gid)

    return resolve


# ═══════════════════════════════════════════════════════════════════════════
# Agent factory construction (emitter/workspace wiring)
# ═══════════════════════════════════════════════════════════════════════════


class _WorkspaceEmitterFactory:
    """Wraps an emitter factory so every created emitter gets a sessions-dir
    provider derived from the workspace resolver cell.

    Keeping the original factory and provider as explicit attributes avoids
    capturing the entire enclosing build scope in a closure.
    """

    __slots__ = ("_orig", "_provider")

    def __init__(
        self,
        orig: TurnEventSinkFactory,
        provider: Callable[[], Path | None],
    ) -> None:
        self._orig = orig
        self._provider = provider

    def __call__(self, binding: TurnBinding) -> TurnEventSink:
        sink = self._orig(binding)
        # The concrete sink may be a channel sink or a composite wrapping
        # one. Both shapes expose set_sessions_dir_provider as a public
        # setter - the composite forwards to its children, so the provider
        # reaches every leaf. Sinks without the setter are passed through
        # unchanged (sink extension boundary).
        setter = getattr(sink, "set_sessions_dir_provider", None)
        if setter is not None:
            setter(self._provider)
        return sink


def _resolve_trace_enabled(app_config: AppConfig | None) -> bool:
    if app_config is None or app_config.observability is None:
        return True
    return app_config.observability.trace_backend != TraceBackend.OFF


def _cell_sessions_dir(cell: WorkspaceManager | None) -> Path | None:
    """Resolve the workspace sessions dir from a resolver cell.

    Returns ``None`` when the cell is not yet materialized so callers fall
    back to the ctxvar-based resolution path.
    """
    if cell is None:
        return None
    try:
        return cell.resolve_workspace().ctx.paths.sessions_dir
    except RuntimeError:
        return None


def _build_agent_factory(
    provider: Any,
    tool_manager: Any,
    inbox_server: Any,
    inbox_consumer: Any,
    shared_hooks: Any,
    shared_hook_runner: Any,
    shared_interceptor_chain: Any,
    control_channel: Any,
    workspace_resolver: WorkspaceManager | None,
    pool_name: str,
    emitter_factory: TurnEventSinkFactory | None,
    *,
    media_store_resolver: Callable[[], MediaStore] | None = None,
    runtime_constructor: AgentFactory | None = None,
    session_registry: SessionRegistry | None = None,
    session_binding_store: SessionBindingStore | None = None,
    approval_declared: bool = False,
) -> Any:
    """Build the pool's runtime constructor, defaulting to the react factory.

    ``runtime_constructor`` (W5 runtime-slot seam): a third-party
    native-component strategy's own loop builder. ``None`` → the bundled
    react factory (``DefaultAgentFactory``). Either way the SAME
    emitter/pool-context wrapper applies — one post-build wiring step for
    every runtime shape (architecture rule 15).

    ``approval_declared``: the pool root's ``approval:`` declaration
    presence — the react factory constructs the approval collaborators
    (resumer/renderer) only under it, so an undeclared boot loads no
    approval implementation. Third-party constructors own their runner
    construction and do not read the flag.
    """
    factory: AgentFactory
    if runtime_constructor is not None:
        factory = runtime_constructor
    else:
        factory = DefaultAgentFactory(
            default_llm_provider=provider,
            default_tool_manager=tool_manager,
            inbox_server=inbox_server,
            inbox_consumer=inbox_consumer,
            default_hooks=shared_hooks,
            default_hook_runner=shared_hook_runner,
            default_interceptor_chain=shared_interceptor_chain,
            control_channel=control_channel,
            session_registry=session_registry,
            approval_declared=approval_declared,
        )

    _orig_create = factory.create_agent

    async def _create_with_emitter(*args: Any, **kwargs: Any) -> Any:
        instance = await _orig_create(*args, **kwargs)
        if instance.pipeline is not None:
            turn_runner = instance.pipeline._turn_runner
            if emitter_factory is not None:
                turn_runner.set_emitter_factory(emitter_factory)
            if workspace_resolver is not None:
                turn_runner.set_pool_context(
                    workspace_manager=workspace_resolver, pool_name=pool_name
                )
            builder = turn_runner.turn_context_builder
            if builder is not None and media_store_resolver is not None:
                builder.media_store_resolver = media_store_resolver
        return instance

    factory.create_agent = _create_with_emitter  # type: ignore[method-assign]
    return factory


# ═══════════════════════════════════════════════════════════════════════════
# Assembly context builder + fallback context manager
# ═══════════════════════════════════════════════════════════════════════════


def _build_assembly_context(
    *,
    pool_name: str,
    pool_spec: PoolSpec,
    peer_links: tuple[PeerLink, ...],
    project_dir: Path,
    data_dir: Path,
    broker: MessageBroker,
    inbox_server: Any,
    agent_bus: Any,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    workspace_handle: WorkspaceHandle | None,
    workspace_resolver: WorkspaceManager | None,
    emitter_factory: TurnEventSinkFactory | None,
    app_config: Any | None,
    persistence: Any | None,
    mcp_registry: McpConnectionRegistry | None,
    shared_hooks: list[Hook],
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    control_origin: str,
    session_registry: SessionRegistry | None,
    session_store: SessionStore | None,
    bot_model_config: ModelRegistry | None,
    model_choice_registry: ModelChoiceRegistry | None,
    model_assembly: PoolModelAssembly,
    record_scope: RecordScope | None,
    command_processor: Any | None,
    control_channel: ControlChannel | None,
    pool_data: PoolDataSnapshot | None,
    assembly_deps: PoolAssemblyDeps,
    root_system_prompt: str = "",
    workspace_resources: Any | None = None,
) -> PoolAssemblyContext:
    """Build the frozen :class:`PoolAssemblyContext` passed to ``strategy.assemble_main``."""
    from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext

    return PoolAssemblyContext(
        pool_name=pool_name,
        pool_spec=pool_spec,
        peer_links=peer_links,
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
        scope_path=ScopePath(
            workspace_root=(
                workspace_handle.current if workspace_handle is not None else project_dir
            ),
            pool_name=pool_name,
        ),
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
        model_assembly=model_assembly,
        record_scope=record_scope,
        control_origin=control_origin,
        command_processor=command_processor,
        control_channel=control_channel,
        pool_data=pool_data,
        on_session_start=None,
        on_session_end=None,
        router=None,
        assembly_deps=assembly_deps,
        root_system_prompt=root_system_prompt,
        workspace_resources=workspace_resources,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Pool construction + strategy-main registration
# ═══════════════════════════════════════════════════════════════════════════


def _build_agent_pool(
    broker: MessageBroker,
    factory: Any,
    agent_bus: Any,
    inbox_consumer: InboxConsumer,
    session_factory: SessionIdFactory,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    pool_name: str,
    *,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
) -> AgentPool:
    from modex_agent.multi_agent import AgentPool

    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        agent_bus=agent_bus,
        inbox_consumer=inbox_consumer,
        session_factory=session_factory,
        safety=safety,
        retention=retention,
        session_registry=session_registry,
        session_store=session_store,
    )
    logger.info("Pool '%s': AgentPool created", pool_name)
    return pool


async def _register_strategy_main(
    pool: AgentPool,
    main: MainAssembly,
    pool_name: str,
    *,
    deps: AgentMaterializeDeps,
) -> None:
    """Register a strategy-built main runtime (external / self-owning shapes).

    The strategy constructed the agent + turn runner + pipeline directly
    (W5 — the ``ExternalAwareFactory`` factory dispatch died); this helper
    performs the orchestrator's two post-build steps: pool registration
    and the SAME emitter/pool-context injection every wrapper-bypassing
    runtime shape gets (``inject_emitter_and_pool_context`` — one
    mechanism, architecture rule 15). The pool's ``AgentMaterializeDeps``
    carries the same workspace-wrapped emitter factory and resolver the
    factory wrapper path uses.
    """
    await pool.register_resident(main.descriptor, main.instance)
    inject_emitter_and_pool_context(main.instance, deps)
    logger.info(
        "Pool '%s': strategy-built main agent '%s' registered",
        pool_name,
        main.descriptor.address.name,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


async def create_pool(
    pool_name: str,
    declared: DeclaredPoolBuild,
    assembly_deps: PoolAssemblyDeps,
    *,
    project_dir: Path,
    data_dir: Path,
    broker: MessageBroker,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    im_ui_factory: Callable[[], ApprovalUserInterface] | None,
    shared_hooks: list[Hook],
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    control_origin: str,
    model_assembly: PoolModelAssembly,
    default_llm_provider_name: str,
    control_channel: ControlChannel | None = None,
    command_processor: Any = None,
    pool_data: PoolDataSnapshot | None = None,
    workspace_handle: WorkspaceHandle | None = None,
    workspace_resolver: WorkspaceManager | None = None,
    media_store_resolver: Callable[[], MediaStore] | None = None,
    # Business sink factory (TurnBinding face); pool name injected below.
    emitter_factory: TurnEventSinkFactory | None = None,
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    # Business callback: (child_id, parent_id, pool); pool-bound below.
    on_subagent_created: Callable[[str, str, str], Awaitable[None]] | None = None,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
    # Threaded onto PoolAssemblyContext for deployment LLM factories (see
    # PoolAssemblyContext.model_choice_registry).
    model_choice_registry: ModelChoiceRegistry | None = None,
    mcp_registry: McpConnectionRegistry | None = None,
    persistence: Any | None = None,
    app_config: Any | None = None,
    strategy_registry: ExecutionStrategyRegistry | None = None,
    # Per-workspace session→pool attribution index; the tree/node stores built
    # below register into it.
    session_pool_index: SessionPoolIndex | None = None,
    # Supply-mode: the caller (the workspace resource factory) pre-fills
    # workspace_registry + workspace_resources to prevent the recursive
    # single-flight deadlock (pipeline.run → WorkspaceMaterializeStage →
    # registry.materialize → re-enter factory body). Required — the
    # pipeline is the single assembly path (no legacy branch).
    workspace_registry: Any,
    workspace_resources: Any,
    # Service-level ComponentRegistry singleton: threaded from the deployment
    # service so every pool resolves against the same factory set. None → a
    # local registry over the bundled FW defaults for framework-style tests.
    component_registry: ComponentRegistry | None = None,
    # The declared workspace resource selection (ticket 14, SPEC §3.1) —
    # carried onto the assembly context chain's workspace layer so
    # factories can read the workspace's declared backend/paths/MCP set.
    # None for pool-as-root and no-declaration boots.
    workspace_spec: WorkspaceSpec | None = None,
    # The pool's storage isolation scope (deployments may subclass RecordScope
    # with business dimensions, e.g. a per-pool dimension). None → RecordScope().
    record_scope: RecordScope | None = None,
) -> PoolInstance:
    """Build one PoolInstance's DEPLOYMENT resources from the declared pool.

    Strategy-agnostic (ADR-0025, ticket 6): resolves the strategy from the
    declared root's ``execution_strategy``, calls ``strategy.assemble_main(ctx)``,
    and runs common post-assembly wiring. ``ProviderUnavailableError`` is
    caught to skip main-agent registration, leaving the pool structurally
    intact for peer routing.
    """
    pool_spec = declared.pool
    main_spec = pool_spec.root_agent
    root_agent_name = main_spec.name

    if record_scope is None:
        record_scope = RecordScope()
    llm_defaults = model_assembly.default_llm_defaults()

    inbox_dir = data_dir / "inbox" / pool_name
    inbox_db_path = data_dir / "state.db"
    inbox_server = build_inbox(
        app_config,
        persistence,
        inbox_dir,
        inbox_db_path,
        record_scope,
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

        # Framework-style fallback: the bundled FW defaults only. The
        # framework cannot anchor a deployment's plugin directory — callers
        # needing deployment plugins pass their loaded ``component_registry``.
        resolved_registry = ComponentRegistry()
        await ComponentRegistryLoader.load(
            resolved_registry,
            PluginDiscoveryConfig(
                bundled_factories=(DefaultPlugin(),),
                project_plugin_paths=(),
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

    _pool_bound_emitter: TurnEventSinkFactory | None = None
    if emitter_factory is not None:

        def pool_bound_emitter(binding: TurnBinding) -> TurnEventSink:
            # The pool assembly layer owns the pool name — stamp it into
            # the binding so channel factories see a complete identity.
            return emitter_factory(binding.model_copy(update={"pool": pool_name}))

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
        control_origin=control_origin,
        session_registry=session_registry,
        session_store=session_store,
        bot_model_config=model_assembly.resolved_config,
        model_choice_registry=model_choice_registry,
        model_assembly=model_assembly,
        record_scope=record_scope,
        command_processor=command_processor,
        control_channel=control_channel,
        pool_data=pool_data,
        assembly_deps=assembly_deps,
        root_system_prompt=system_prompt,
        workspace_resources=workspace_resources,
    )

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
        component_registry=resolved_registry,
        assembly_spec=main_assembly_spec,
    )

    session_binding_store = InMemorySessionBindingStore()

    # C1 sub path: the subagent-default provider slot resolves once here
    # (hoisted before the pipeline, ticket 09) — it now only gates whether
    # this pool HAS an LLM slot. Ownership-derived (W5): strategies that
    # own their model config declare ``needs_llm_provider=False``.
    sub_default_llm_provider: LLMProvider | None = None
    if strategy.ownership.needs_llm_provider:
        sub_default_llm_provider = await _resolve_llm_slot(
            resolved_registry,
            default_llm_provider_name,
            {},
            ctx,
            workspace_ctx_for_spec,
        )

    # Default-model pin (ADR-0050, D-5 re-based + D-6): everything that is
    # neither the main agent's per-turn selection nor an explicit agent pin
    # runs on the DEFAULT model — derived from the deployment LLM SLOT product
    # (a per-turn proxy in production is wrapped into a pin that ignores the
    # turn's ContextVar; a custom slot product passes through — the LLM slot
    # stays the extension point). Unconfigured subagents materialize with it
    # (their turns are InboxPoller-dispatched tasks where the turn-scoped
    # ContextVar never arrives, so caller-inheritance was structurally
    # impossible); supply-side workers (the experience reviewer) use it as
    # the review provider. The model assembly seam shares ONE real-provider
    # cache between this pin and the declared agent pins (same assembly
    # point). Strategies owning their model config keep None.
    default_pinned_provider: LLMProvider | None = None
    if sub_default_llm_provider is not None:
        default_pinned_provider = model_assembly.pinned_default_provider(
            sub_default_llm_provider
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
    # Pre-pipeline: the capability supply aggregation (Stage 3) reads the
    # pool's template registry handle when building the subagents supply.
    pool.template_registry = template_registry
    scope_path = ctx.scope_path
    if scope_path is None:
        # _build_assembly_context always carries the scope path — reaching
        # here means a caller built the context outside create_pool.
        raise RuntimeError(
            f"Pool {pool_name!r}: create_pool requires a scope_path on the "
            "pool assembly context (workspace root + pool name carrier)"
        )
    context_fork_builder = ContextForkBuilder()

    poller = InboxPoller(pool, interval=0.2)
    pool.attach_poller(poller)
    agent_bus.set_poller(poller)

    tree_store, node_store, track_store = build_session_tree_stores(
        app_config,
        persistence,
        data_dir / "session_tree" / pool_name,
        record_scope,
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
        workspace_root=str(scope_path.workspace_root),
        session_registry=session_registry or InMemorySessionRegistry(),
        binding_store=session_binding_store,
    )
    inbox_consumer.set_on_consumed(tree_manager.on_consumed)
    poller.attach_tree_manager(tree_manager)
    # Pre-pipeline: PoolAssembleStage reads the pool's tree handle for
    # the capability supply views (and its PoolRuntimeDeps
    # session_tree_manager) — the setter is idempotent with the
    # post-pipeline materialize_deps assignment (the same tree object).
    pool.tree = tree_manager

    # C1 main path: the main agent's LLM_PROVIDER name resolves to an
    # instance exactly once here, feeding BOTH the agent factory and the
    # Stage-4 assembly inputs. Strategies that own their model config
    # (ownership.needs_llm_provider=False) keep None.
    main_llm_provider: LLMProvider | None = None
    if strategy.ownership.needs_llm_provider:
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

    # The root agent's bound skill resolver (plan §11.3.1): looked up from
    # the pool's aggregated capability supply — Stage 3 builds the supply,
    # Stage 4 threads the resolver into the factory + inputs. None when the
    # skills capability is vetoed or absent for this pool.
    skill_resolver: SkillResolver | None = None

    factory = None

    _workspace_emitter_factory = _pool_bound_emitter
    if _pool_bound_emitter is not None and workspace_resolver is not None:
        _workspace_emitter_factory = _WorkspaceEmitterFactory(
            _pool_bound_emitter,
            lambda: _cell_sessions_dir(workspace_resolver),
        )

    def build_native_inputs(
        spec: AssemblySpec,
        builder: AssemblyBuilder,
        _assembly_context: AssemblyContext,
    ) -> NativeAssemblyInputs:
        nonlocal factory, main_provider, skill_resolver
        strategy_result = builder.strategy_result
        if strategy_result is None:
            raise RuntimeError("Native Stage4 requires the Stage3 strategy result")
        # Stage 3 aggregated the capability supply onto the propagated
        # context; the root resolver is a LOOKUP, not a construction
        # (plan §11.3.1). Only an explicit capability veto leaves the
        # resolver None; active Skills wiring requires a valid supply.
        propagated = builder.propagated_context
        pool_runtime = (
            propagated.pool_runtime
            if propagated is not None
            else None
        )
        if not any(
            capability.name == SKILLS_CAPABILITY_NAME
            for capability in spec.capabilities
        ):
            skill_resolver = None
        else:
            if pool_runtime is None:
                raise RuntimeError("Native Stage4 requires pool runtime dependencies")
            skill_resolver = require_skills_supply(
                pool_runtime.capability_supply
            ).resolver_for(root_agent_name)
        provider = main_llm_provider
        cassette_recorder = (
            strategy_result.react_products.cassette_recorder
            if strategy_result.react_products is not None
            else None
        )
        if cassette_recorder is not None:
            provider = cassette_recorder.wrap_provider(provider)
        main_provider = provider
        # Ownership-derived skips (W5 completion — the contract, not
        # strategy internals, decides): a strategy declaring
        # ``needs_memory=False`` takes no framework memory products (no
        # memory system, no memory-backed context manager); one declaring
        # ``owns_context=True`` takes no framework context-manager product
        # at all — it assembles its own context (external already
        # self-builds; third native loops get the same contract here).
        needs_memory = strategy.ownership.needs_memory
        owns_context = strategy.ownership.owns_context
        # The runtime-constructor seam (W5): a strategy that supplied its
        # own loop builder wins; otherwise the bundled react factory.
        factory = _build_agent_factory(
            provider,
            strategy_result.tool_manager,
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
            runtime_constructor=strategy_result.runtime_constructor,
            session_registry=session_registry,
            session_binding_store=session_binding_store,
            approval_declared=main_spec.approval is not None,
        )
        pool._agent_factory = factory
        return NativeAssemblyInputs(
            agent_factory=factory,
            broker=broker,
            llm_defaults=llm_defaults,
            pool=pool,
            context_manager=(
                strategy_result.context_manager
                if needs_memory and not owns_context
                else None
            ),
            memory_system=(
                pool_data.context_manager.memory_system
                if pool_data is not None and needs_memory
                else None
            ),
            memory_config=assembly_deps.memory,
            llm_provider=provider,
            needs_llm_provider=strategy.ownership.needs_llm_provider,
            tool_manager=strategy_result.tool_manager,
            skill_resolver=skill_resolver,
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
    # their runtime deps from the chain — the notification service rides
    # SupplyInfra into PoolRuntimeDeps, and the D-6 default-model PIN rides
    # SupplyInfra into the capability supply views (the experience reviewer
    # builds on it — stable default model, never the triggering turn's
    # choice).
    notification_service = AgentNotificationService(
        output_adapter=output_adapter,
    )
    infra = SupplyInfra(
        pool_assembly_ctx=ctx,
        pool=pool,
        # The pool's COMPLETE compiled spec set (root + subagents) — Stage 3
        # aggregates the capability supply over exactly this set (SPEC
        # §7.1), so capabilities effective only on subagents still get their
        # pool-level supply.
        pool_specs=(
            declared.root.spec,
            *(agent.spec for agent in declared.subagents),
        ),
        notification_service=notification_service,
        default_llm_provider=default_pinned_provider,
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

    tool_manager: ToolManager | None
    context_manager: ContextManager | None
    cassette_recorder: CassetteRecorder | None
    root_provider: WorkspaceRootProvider | None
    component_hook_specs: tuple[HookSpec, ...]
    if assembly is not None:
        tool_manager = assembly.tool_manager
        context_manager = assembly.context_manager
        root_provider = assembly.root_provider
        if assembly.react_products is not None:
            cassette_recorder = assembly.react_products.cassette_recorder
            component_hook_specs = assembly.react_products.component_hook_specs
        else:
            cassette_recorder = None
            component_hook_specs = ()
    else:
        tool_manager = None
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
    # Strategy-built mains (external / self-owning shapes; native mains built
    # the factory in Stage 4): no provider — the strategy owns its runtime.
    if factory is None:
        factory = _build_agent_factory(
            None,
            tool_manager,
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
            session_registry=session_registry,
            session_binding_store=session_binding_store,
            approval_declared=main_spec.approval is not None,
        )
    pool._agent_factory = factory  # type: ignore[attr-defined]

    # The react strategy always fills ``context_manager`` (pool data or its
    # own fallback); the retired create_pool-level fallback served the
    # external registration path, which the strategy-owned main build
    # replaced (W5) — external mains carry their own minimal context
    # manager inside ``assemble_external_pipeline``.

    if tool_manager is None:
        from modex_agent.tools.manager import InMemoryToolManager

        tool_manager = InMemoryToolManager()

    subagent_store_registry = None
    if pool_data is not None:
        ms = pool_data.context_manager.memory_system
        if ms is not None:
            subagent_store_registry = ms.store_registry

    # The lazy graph-context closure shared by the main pipeline AND the
    # subagent materialization deps (ticket 12 — one resolver, both paths).
    graph_context_resolver = (
        _make_graph_context_resolver(workspace_resolver) if workspace_resolver is not None else None
    )

    # The pool's aggregated capability supply (built once by Stage 3 over
    # pool_specs) rides the pipeline product onto the subagent
    # materialization path — main PoolRuntimeDeps and AgentMaterializeDeps
    # carry the SAME mapping (SPEC §7.1).
    propagated = assembled.propagated_context if assembled is not None else None
    capability_supply = (
        propagated.pool_runtime.capability_supply
        if propagated is not None and propagated.pool_runtime is not None
        else {}
    )
    approval_audit_store = build_approval_audit_store(
        app_config, persistence, record_scope,
    )
    # D-5 per-agent model pins: declared ``model`` references resolve against
    # the deployment model universe ONCE here — the single assembly-layer
    # resolution point (through the model-assembly seam). The framework
    # template/materialize consumes the resolved values blind.
    agent_llm_pins = model_assembly.resolve_agent_pins(pool_spec)
    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=session_factory,
        broker=broker,
        tree=tree_manager,
        safety=safety,
        llm_model=llm_defaults.model,
        llm_temperature=llm_defaults.temperature,
        llm_max_output_tokens=llm_defaults.max_output_tokens,
        llm_reasoning_effort=llm_defaults.reasoning_effort,
        llm_model_info=llm_defaults.model_info,
        # D-5 re-based: the materialization default is the default-model
        # pinned provider — unconfigured subagents run the DEFAULT model
        # (ADR-0050), with LlmDefaults above already matching it.
        llm_provider=default_pinned_provider,
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
        workspace_resources=workspace_resources,
        mcp_registry=mcp_registry,
        execution_strategy=strategy,
        strategy_registry=registry,
        materializer=SubagentMaterializer(),
        data_dir=data_dir,
        app_config=app_config,
        persistence=persistence,
        emitter_factory=_workspace_emitter_factory,
        control_origin=ctx.control_origin,
        default_llm_provider=default_llm_provider_name,
        memory_store_registry=subagent_store_registry,
        component_registry=resolved_registry,
        pool_assembly_ctx=ctx,
        graph_context_resolver=graph_context_resolver,
        capability_supply=capability_supply,
        approval_audit=approval_audit_store,
        agent_llm_pins=agent_llm_pins,
        record_scope=record_scope,
    )
    pool.materialize_deps = deps
    pool.pool_name = pool_name
    pool.context_fork_builder = context_fork_builder

    # Main-agent registration. Native mains registered inside the pipeline
    # (Stage 4); strategy-built mains (external / self-owning shapes — the
    # strategy returned ``main``) register here — before tree recovery +
    # poller start, so no pending message races an unregistered main.
    if not provider_available:
        logger.warning("Pool '%s': main agent registration skipped", pool_name)
    elif assembly is not None and assembly.main is not None:
        await _register_strategy_main(pool, assembly.main, pool_name, deps=deps)

    # Recover stale session-tree state BEFORE starting the poller — recovery
    # of stale terminal nodes and pending-input rebuilds must complete before
    # dispatch begins (todo 19).
    for record in await tree_store.list_active():
        await tree_manager.recover_tree(record.tree_id)

    # Start the poller AFTER main agent registration to eliminate the startup
    # race where pending messages from a previous run are dispatched before
    # the main agent is ready — causing "no template for X; skipping".
    pool.start_poller()

    # The memory-runner hooks are NOT injected here anymore. The historical
    # unconditional ``TodoReorientationHook`` registration died with the todo
    # supply convergence (SPEC §8.2 B2): the ``todo`` capability contributes
    # ``todo_reorientation`` as a roster entry, and Stage 4's
    # ``dispatch_hooks`` memory branch registers it on the pool's memory
    # system — for native mains with the todo capability; external mains
    # (no Stage 4, and external agents can never declare capabilities) get
    # none, which is behavior-neutral since their memory system never fires
    # cleanup. ``user_notice_cleanup`` was already roster-dispatched only.

    # The pool's communication faces (the retired pre-pipeline BIZ
    # construction died with the subagents supply wave, SPEC §8.4): the
    # ``subagents`` capability's supply carries the service and the root's
    # native assembly carries the per-agent target store (its wiring
    # artifact — peer targets join the SAME store at workspace materialize
    # time). Capability-less pools (external pools, lone roots) fall back
    # to the FW builder — the single construction authority the capability
    # supply itself uses — so the control-facade/modexctl plane keeps a
    # router for every pool.
    subagents_supply = capability_supply.get("subagents")
    if isinstance(subagents_supply, SubagentsSupply):
        main_service = subagents_supply.service
    else:
        main_service = build_pool_communication_service(
            root_agent_name=root_agent_name,
            pool=pool,
            tree=tree_manager,
            pool_name=pool_name,
            project_dir=project_dir,
            session_registry=session_registry,
            template_registry=template_registry,
            scope_path=scope_path,
            workspace_manager=workspace_resolver,
            trace_enabled=_resolve_trace_enabled(app_config),
        )
    subagents_wiring = (
        (assembled.capability_wirings or {}).get("subagents") if assembled is not None else None
    )
    main_store = (
        subagents_wiring.artifacts.get("target_store") if subagents_wiring is not None else None
    ) or CommunicationTargetStore()
    logger.info(
        "Pool '%s': communication store (%d targets)",
        pool_name,
        len(main_store.list()),
    )

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
        extensions_pool_runtime.interceptor_chain if extensions_pool_runtime is not None else None
    ) or shared_interceptor_chain
    pool_command_processor = (
        (extensions_pool_runtime.command_processor if extensions_pool_runtime is not None else None)
        or command_processor
        or SlashCommandProcessor.default()
    )

    if strategy.ownership.needs_main_agent_tools:
        # The main agent's memory-backed context manager — the same instance
        # its load() resolves through; governance compaction must address the
        # same MemoryContext (narrowing from the ContextManager ABC follows
        # the native_core.py:551 precedent at this assembly seam).
        main_memory_context_manager = (
            context_manager
            if isinstance(context_manager, MemorySystemContextManager)
            else None
        )
        wire_main_pipeline(
            pool,
            root_agent_name,
            inbox_consumer,
            notification_service,
            pool_interceptor_chain,
            im_ui_factory,
            main_spec,
            assembly_deps,
            project_dir,
            pool_command_processor,
            pool_name,
            tool_manager=tool_manager,
            pool_spec=pool_spec,
            peer_links=declared.peer_links,
            root_provider=root_provider,
            model_info=llm_defaults.model_info,
            cassette_recorder=cassette_recorder,
            graph_context_resolver=graph_context_resolver,
            session_binding_store=session_binding_store,
            component_hook_specs=component_hook_specs,
            approval_audit_store=approval_audit_store,
            memory_context_manager=main_memory_context_manager,
        )
    else:
        # Self-owning shapes (ownership.needs_main_agent_tools=False): the
        # strategy-built agent has no native tool surface (it communicates
        # via its own mechanism, e.g. ``modexctl send`` CLI, so the derived
        # communication entries are absent from its compiled spec) and no
        # react-only ``wire_main_pipeline`` (governance/approval/hooks).
        # Only set ``command_processor`` on the pipeline so pre-lock ``/stop``
        # dispatch still works.
        if pool_command_processor is not None:
            main_instance = pool._agents.get(root_agent_name)
            if main_instance is not None and main_instance.pipeline is not None:
                main_instance.pipeline.command_processor = pool_command_processor
        logger.info(
            "Pool '%s': strategy %r — skipped wire_main_pipeline",
            pool_name,
            strategy.name,
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
        skill_resolver=skill_resolver,
        mcp_manager=mcp_manager,
        root_agent_name=root_agent_name,
        main_execution_strategy=strategy_name_of(main_spec.execution_strategy),
        provider=main_provider,
        notification_service=notification_service,
        communication_service=main_service,
        tree_manager=tree_manager,
        target_store=main_store,
        session_binding_store=session_binding_store,
        requires_main_agent_tools=strategy.ownership.needs_main_agent_tools,
        roster_hook_names=roster_hook_names,
        comm_tools_derived=True,
    )
