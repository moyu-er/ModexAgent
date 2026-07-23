"""Pool builder - IOC-style factory that builds one PoolInstance from PoolSpec.

Each build step is a focused method.  Convention over configuration:
config drives behaviour; methods read from PoolSpec / MainAgentSpec with
sensible defaults.  No giant if-else chains.

Ticket 6 (ADR-0025): the strategy-specific ``_build_*`` helpers
(``_build_llm_provider``, ``_build_terminal_manager``, ``_build_tools``,
``_build_skill_manager``, ``_resolve_cassette_config``,
``_fallback_context_manager``, ``_cell_sessions_dir``) moved into the shared
:class:`bot.service._assembly_helpers._PoolAssemblyMixin`, inherited by both
:class:`ReactExecutionStrategy` and :class:`ExternalCodingExecutionStrategy`.
``create_pool`` is now strategy-agnostic: it resolves the strategy, calls
``strategy.assemble(ctx)``, and runs the common post-assembly wiring
(register main agent, communication, pipeline construction via factory).
Both react and external_coding pools follow the same code path here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.messaging import MessageBroker

if TYPE_CHECKING:
    # ``WorkspaceHandle`` / ``WorkspaceResolverCell`` live in the bundle,
    # which is imported by BotService via this module; deferring them to
    # TYPE_CHECKING keeps the import graph acyclic. Runtime references
    # (``WorkspaceHandleRootProvider``) are imported lazily inside
    # ``create_pool`` for the same reason.
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.handle import (
        WorkspaceHandle,
        WorkspaceResolverCell,
    )
    from modex_agent.memory.cleanup import CleanupResult
    from modex_agent.memory.core.models import CompressionReason
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
    )
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

from bot.config.memory_defaults import subagent_memory
from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.scope import MemoryContext
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.core.tool_manager import ToolManager
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.notification import (
    AgentNotificationService,
    MaxIterationNotifyHook,
    TurnOutcomeNotifyHook,
)
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.memory.cleanup_events import MemoryCleanupListener
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.messaging.broker_bridge import BrokerBridgeService, OutputRoute
from modex_agent.multi_agent import (
    AgentPool,
    DefaultAgentFactory,
    SessionRetentionPolicy,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import LocalAgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.context_fork import ContextForkBuilder
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.producer import InboxProducer
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.pool_config import PoolAssemblyDeps, PoolStore
from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToAgentTool,
)
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.tools.presets import (
    ToolPreset,
    ToolSupplement,
    get_preset_tools,
    get_supplement_tools,
)
from modex_agent.tools.terminal import SubprocessExecutor, SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
)

from .builders import build_inbox, resolve_system_prompt
from ._assembly_helpers import _resolved_or_placeholder
from .external_coding_strategy import (
    ExternalCodingAwareFactory,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


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
        app_config, persistence, inbox_dir, inbox_db_path, pool_name,
    )
    inbox_producer = InboxProducer(server=inbox_server)
    inbox_consumer = InboxConsumer(server=inbox_server)
    agent_bus = LocalAgentMessageBus(
        producer=inbox_producer, consumer=inbox_consumer, broker=broker
    )

    registry = strategy_registry
    if registry is None:
        registry = _default_strategy_registry()
    strategy_name = (
        main_spec.execution_strategy.value
        if main_spec.execution_strategy
        in (ExecutionStrategyKind.REACT, ExecutionStrategyKind.EXTERNAL_CODING)
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
        assembly_deps=assembly_deps,
    )

    if pool_data is not None:
        await ensure_long_term_defaults(
            project_dir, assembly_deps.memory, pool_data.context_manager.memory_system
        )

    provider_available = True
    external_coding_deps: dict[str, Any] | None = None
    try:
        assembly = await strategy.assemble(ctx)
    except ProviderUnavailableError as exc:
        logger.warning(
            "Pool '%s': external_coding provider %r not found on PATH; "
            "skipping pool registration",
            pool_name, exc.executable,
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
        if assembly.external_coding_deps is not None:
            external_coding_deps = assembly.external_coding_deps
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

    # Wrap before _build_agent_factory AND AgentMaterializeDeps so both the
    # main-agent _create_with_emitter path and the external-subagent
    # BotSubagentExternalCodingBuilder path receive the same wrapped factory.
    if emitter_factory is not None and workspace_resolver is not None:
        emitter_factory = _WorkspaceEmitterFactory(
            emitter_factory,
            lambda: _cell_sessions_dir(workspace_resolver),
        )

    factory = _build_agent_factory(
        provider, tool_manager, skill_manager,
        inbox_server, shared_hooks, shared_hook_runner,
        shared_interceptor_chain, control_channel,
        workspace_resolver, pool_name, emitter_factory,
        external_coding_deps=external_coding_deps,
        observability_config=app_config.observability if app_config is not None else None,
    )
    session_factory = SessionIdFactory()

    if context_manager is None:
        context_manager = _fallback_context_manager(main_spec, system_prompt)
    pool = _build_agent_pool(
        broker, factory, context_manager, agent_bus,
        inbox_consumer, session_factory, safety, retention, pool_name,
        session_registry=session_registry, session_store=session_store,
    )

    template_registry = AgentTemplateRegistry(
        PoolStore(base_dir=project_dir), default_subagent_memory=subagent_memory(),
    )
    templates = template_registry.list_templates(pool_name)
    logger.info("Pool '%s': %d subagent templates available", pool_name, len(templates))
    fallback_runtime_dir = data_dir / "runtime_state" / pool_name
    fallback_runtime_dir.mkdir(parents=True, exist_ok=True)
    path_resolver = WorkspacePathResolver(
        workspace_manager=workspace_resolver, pool_name=pool_name,
        fallback_runtime_dir=fallback_runtime_dir,
    )
    context_fork_builder = ContextForkBuilder()

    notification_service = AgentNotificationService(
        output_adapter=output_adapter, agent_bus=agent_bus,
        parent_agent_name=main_agent_name,
    )

    if tool_manager is None:
        from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig

        tool_manager = InMemoryToolManager(config=ToolManagerConfig())

    # ADR-0027 T8: inject a SubagentExternalCodingBuilder iff this pool
    # declares at least one external_coding subagent. React-only pools
    # leave it None — AgentTemplate._materialize_external raises only when
    # an EXTERNAL_CODING subagent is dispatched without a builder.
    subagent_external_coding_builder = _maybe_build_external_subagent_builder(
        pool_spec=pool_spec,
        pool_name=pool_name,
        project_dir=project_dir,
        data_dir=data_dir,
        app_config=app_config,
        persistence=persistence,
    )

    deps = AgentMaterializeDeps(
        agent_factory=factory, pool=pool, session_factory=session_factory,
        broker=broker, safety=safety,
        llm_model=default_resolved.model.model,
        llm_temperature=default_resolved.model.temperature,
        llm_max_output_tokens=default_resolved.model.max_output_tokens,
        llm_reasoning_effort=default_resolved.model.reasoning_effort,
        project_dir=project_dir, notification_service=notification_service,
        inbox_consumer=inbox_consumer, agent_bus=agent_bus,
        output_adapter_factory=output_adapter_factory,
        root_provider=root_provider, session_registry=session_registry,
        on_subagent_created=on_subagent_created,
        context_fork_builder=context_fork_builder,
        workspace_path_resolver=path_resolver, mcp_registry=mcp_registry,
        todo_store=todo_store, trace_enabled=_resolve_trace_enabled(app_config),
        subagent_external_coding_builder=subagent_external_coding_builder,
        emitter_factory=emitter_factory,
    )
    pool.materialize_deps = deps
    pool.template_registry = template_registry
    pool.pool_name = pool_name
    pool.context_fork_builder = context_fork_builder

    from modex_agent.multi_agent.inbox_poller import InboxPoller

    poller = InboxPoller(pool, interval=0.2)
    pool.attach_poller(poller)
    pool.start_poller()

    if provider_available:
        await _register_main_agent(
            pool, main_spec, assembly_deps, system_prompt, safety, pool_name,
            factory=factory, broker=broker, context_manager=context_manager,
            bot_model_config=bot_model_config,
        )
    else:
        logger.warning("Pool '%s': main agent registration skipped", pool_name)

    if pool_data is not None:
        memory_system = pool_data.context_manager.memory_system
        if memory_system is not None:
            memory_system.add_cleanup_listener(UserNoticeCleanupListener(notification_service))

    main_service, main_store = _build_communication(
        pool, main_agent_name, broker, agent_bus,
        project_dir, pool_name, templates, template_registry,
        session_registry=session_registry, workspace_path_resolver=path_resolver,
        trace_enabled=_resolve_trace_enabled(app_config),
    )
    main_service._target_store = main_store

    if strategy.requires_main_agent_tools:
        tool_manager.register(
            SendToAgentTool(
                store=main_store, source=AgentAddress(name=main_agent_name),
                broker=broker, registry=pool, agent_bus=agent_bus, service=main_service,
            )
        )
        logger.info("Pool '%s': communication tool registered", pool_name)

        _wire_main_pipeline(
            pool, main_agent_name, inbox_consumer, notification_service,
            shared_interceptor_chain, im_ui, main_spec, assembly_deps, project_dir,
            command_processor, pool_name, tool_manager=tool_manager,
            root_provider=root_provider, bot_model_config=bot_model_config,
            model_choice_registry=model_choice_registry,
            cassette_recorder=cassette_recorder,
        )
    else:
        # external_coding path: the external agent has no tool surface and
        # communicates via ``modexctl send`` CLI (not ``send_to_agent``), so
        # skip SendToAgentTool registration and the react-only
        # ``_wire_main_pipeline`` (governance/approval/hooks). Only set
        # ``command_processor`` on the pipeline so pre-lock ``/stop``
        # dispatch still works.
        if command_processor is not None:
            main_instance = pool._agents.get(main_agent_name)
            if main_instance is not None and main_instance.pipeline is not None:
                main_instance.pipeline.command_processor = command_processor
        logger.info(
            "Pool '%s': external_coding — skipped send_to_agent + _wire_main_pipeline",
            pool_name,
        )

    bridge = BrokerBridgeService(
        broker=broker, input_bindings={},
        output_routes=[
            OutputRoute(adapter=output_adapter, match_topic=f"agent:{main_agent_name}:out"),
        ],
    )

    return PoolInstance(
        name=pool_name, media=assembly_deps.media,
        subagent_count=len(pool_spec.subagents), pool=pool, broker_bridge=bridge,
        tool_manager=tool_manager, skill_manager=skill_manager,
        mcp_manager=mcp_manager, terminal_manager=terminal_manager,
        main_agent_name=main_agent_name, provider=provider,
        notification_service=notification_service, communication_service=main_service,
        agent_bus=agent_bus, target_store=main_store,
    )


def _build_assembly_context(
    *,
    pool_name: str,
    pool_spec: PoolSpec,
    project_dir: Path,
    data_dir: Path,
    broker: Any,
    inbox_server: Any,
    agent_bus: Any,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    workspace_handle: WorkspaceHandle | None,
    workspace_resolver: WorkspaceResolverCell | None,
    emitter_factory: Callable[[str], ContentEmitter] | None,
    app_config: Any | None,
    persistence: Any | None,
    mcp_registry: McpConnectionRegistry | None,
    shared_hooks: list,
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    session_registry: SessionRegistry | None,
    session_store: SessionStore | None,
    bot_model_config: BotModelConfig | None,
    model_choice_registry: ModelChoiceRegistry,
    command_processor: Any | None,
    control_channel: InMemoryControlChannel | None,
    pool_data: PoolDataSnapshot | None,
    transcript_store: TranscriptStore | None,
    assembly_deps: PoolAssemblyDeps,
) -> PoolAssemblyContext:
    """Build the frozen :class:`PoolAssemblyContext` passed to ``strategy.assemble``."""
    return PoolAssemblyContext(
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
        registry=None,  # type: ignore[arg-type]
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
        on_session_start=None,
        on_session_end=None,
        router=None,
        assembly_deps=assembly_deps,
    )


def _fallback_context_manager(main_spec: MainAgentSpec, system_prompt: str) -> Any:
    """A minimal context_manager for tests / non-workspace wiring.

    The main agent's real context manager comes from the workspace pool_data;
    this fallback keeps create_pool callable without a workspace (used by
    unit tests that mock the build steps).

    Duplicated from :mod:`bot.service._assembly_helpers` (ticket 6: "Duplicate
    the tiny helper") because ``create_pool`` needs it for the
    provider-unavailable path (when the strategy did not produce a context
    manager) and we do not want ``create_pool`` to import from the strategy
    module.
    """

    from modex_agent.memory.injection import FullInjectionPolicy
    from modex_agent.memory.system import MemorySystemContextManager

    return MemorySystemContextManager(
        memory_system=None,
        default_agent_id=main_spec.agent_name,
        default_agent_role="main",
        base_system_prompt=system_prompt,
        injection_policy=FullInjectionPolicy(pruned_manager=None),
        experience_manager=None,
        roles=list(main_spec.roles),
    )


def _maybe_build_external_subagent_builder(
    *,
    pool_spec: PoolSpec,
    pool_name: str,
    project_dir: Path,
    data_dir: Path,
    app_config: Any | None,
    persistence: Any | None,
) -> Any | None:
    """Construct a ``BotSubagentExternalCodingBuilder`` iff this pool has external subagents.

    Returns ``None`` for react-only pools so ``AgentMaterializeDeps``
    leaves ``subagent_external_coding_builder=None`` (zero overhead —
    ``AgentTemplate.materialize`` never touches the field on the react
    path). When at least one subagent declares
    ``execution_strategy=EXTERNAL_CODING``, returns a pool-scoped builder
    that per-invocation assembles a fully-wired ``ExternalCodingAgent``
    subagent (T8).
    """
    has_external = any(
        sub.execution_strategy == ExecutionStrategyKind.EXTERNAL_CODING
        for sub in pool_spec.subagents
    )
    if not has_external:
        return None
    from bot.service.subagent_external_coding_builder import (
        BotSubagentExternalCodingBuilder,
    )

    return BotSubagentExternalCodingBuilder(
        pool_name=pool_name,
        project_dir=project_dir,
        data_dir=data_dir,
        app_config=app_config,
        persistence=persistence,
    )


def _default_strategy_registry() -> ExecutionStrategyRegistry:
    """Build a registry with both shipped strategies registered.

    Used when ``create_pool`` is called without an explicit
    ``strategy_registry`` (e.g. unit tests, legacy callers). Production wiring
    goes through ``BotService.initialize()`` which builds its own registry
    with the same strategies and threads it through ``wiring.py``.
    """
    from bot.service.external_coding_strategy import (
        ExternalCodingExecutionStrategy,
    )
    from bot.service.react_strategy import ReactExecutionStrategy
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategyRegistry,
    )

    registry = ExecutionStrategyRegistry()
    registry.register(ReactExecutionStrategy())
    registry.register(ExternalCodingExecutionStrategy())
    return registry


# Tiny model-config helpers imported from _assembly_helpers (ticket 6 dedup).

# ═══════════════════════════════════════════════════════════════════════════
# Memory defaults helper
# ═══════════════════════════════════════════════════════════════════════════


async def ensure_long_term_defaults(
    project_dir: Path,
    memory_cfg: MemoryConfig | None,
    memory_system: DefaultMemorySystem,
) -> None:
    """Initialize default long-term memory files if core memory is enabled.

    Supports both old ``long_term`` config (deprecated) and new ``core``
    config. Template paths in config are relative to the project directory.
    Resolves them to absolute paths before calling ``ensure_defaults`` so
    the core memory layer finds templates regardless of CWD (critical after
    ``/cd`` switches the conversation to a different workspace).
    """
    if memory_cfg is None:
        return

    core_enabled = False
    if memory_cfg.long_term is not None and memory_cfg.long_term.enabled:
        core_enabled = True
    if memory_cfg.core is not None and memory_cfg.core.enabled:
        core_enabled = True
    if not core_enabled:
        return

    lt_mgr = memory_system.core_memory_manager
    if lt_mgr is None:
        return

    raw_template_dir: str | None = None
    if memory_cfg.core is not None:
        raw_template_dir = memory_cfg.core.default_templates_dir
    if not raw_template_dir and memory_cfg.long_term is not None:
        raw_template_dir = memory_cfg.long_term.default_templates_dir
    if raw_template_dir:
        abs_template_dir = str((project_dir / raw_template_dir).resolve())
        lt_mgr._config = lt_mgr._config.model_copy(
            update={"default_templates_dir": abs_template_dir}
        )

    defaults: dict[str, str] = {
        "soul": (
            "## 沟通风格\n"
            "- 使用中文回复，风格自然、简洁\n"
            "- 优先给出直接答案，再补充解释\n"
            "- 不确定的事情如实说明，不编造\n"
        ),
        "user": (
            "## 用户画像\n"
            "- 首次使用，暂无特定偏好记录\n"
            "- 后续对话中会逐渐积累用户习惯和偏好\n"
        ),
        "memory": (
            "## 相关知识\n"
            "- 暂无特定领域知识记录\n"
            "- 长期对话中会自动整理和更新\n"
        ),
    }

    ctx = MemoryContext(session_id="default", user_id="default")
    await lt_mgr.ensure_defaults(ctx, defaults)
    print("   [OK] Long-term memory defaults ensured")


# ═══════════════════════════════════════════════════════════════════════════
# Main agent tool-name resolver (pure, for parity testing)
# ═══════════════════════════════════════════════════════════════════════════


def build_main_agent_tool_names(
    tool_preset: str,
    supplements: list[str],
    use_terminal: bool,
) -> set[str]:
    """Return the set of tool NAMES the main agent will receive.

    Pure projection of the main-agent tool assembly (Task 1.6 parity
    helper). Mirrors :func:`_PoolAssemblyMixin._build_tools` + ``send_to_agent``:
    preset-gated file/search/bash + supplement tools (e.g. ast_grep) +
    terminal tools (when ``use_terminal``) + the always-on send_to_agent.
    Bot-specific tools (send_file_to_user, todo, experience) and MCP tools
    are excluded from this projection - they are runtime/path-dependent and
    not governed by the preset/supplement policy.
    """
    names: set[str] = set()
    preset = ToolPreset(tool_preset)

    def _make_bash() -> Any:
        return SubprocessTool(executor=SubprocessExecutor(), timeout=300)

    # File/search/bash tool names per preset. The factory mirrors
    # _build_tools' _make_bash so the bash name surfaces for
    # FULL/READ_WRITE/READ_ONLY.
    for tool in get_preset_tools(preset, subprocess_tool_factory=_make_bash):
        names.add(tool.name)
    for tool in get_supplement_tools([ToolSupplement(s) for s in supplements]):
        names.add(tool.name)
    if use_terminal:
        # Real terminal tool names: CommandTool.name="bash" (already in names
        # via the preset factory above), ProcessTool.name="process",
        # TerminalTool.name="terminal".
        names |= {"bash", "process", "terminal"}
    names.add("send_to_agent")
    return names


# ═══════════════════════════════════════════════════════════════════════════
# Agent factory
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
        orig: Callable[[str], Any],
        provider: Callable[[], Path | None],
    ) -> None:
        self._orig = orig
        self._provider = provider

    def __call__(self, session_id: str) -> Any:
        emitter = self._orig(session_id)
        # The concrete emitter may be a WebBotEmitter or a CompositeEmitter
        # wrapping one. Both types expose set_sessions_dir_provider as a
        # public setter - CompositeEmitter forwards to its children, so the
        # provider reaches every WebBotEmitter leaf.
        setter = getattr(emitter, "set_sessions_dir_provider", None)
        if setter is not None:
            setter(self._provider)
        return emitter


def _resolve_trace_enabled(app_config: AppConfig | None) -> bool:
    if app_config is None or app_config.observability is None:
        return True
    return app_config.observability.trace_backend != TraceBackend.OFF


def _cell_sessions_dir(cell: WorkspaceResolverCell | None) -> Path | None:
    """Resolve the workspace sessions dir from a resolver cell.

    Duplicated from :mod:`bot.service._assembly_helpers` (ticket 6: "Duplicate
    the tiny helper") because ``_build_agent_factory`` uses it for the emitter
    factory wrapper.
    """
    if cell is None:
        return None
    try:
        return cell.resolve_workspace().ctx.paths.sessions_dir
    except RuntimeError:
        return None


def _build_agent_factory(
    provider,
    tool_manager,
    skill_manager,
    inbox_server,
    shared_hooks,
    shared_hook_runner,
    shared_interceptor_chain,
    control_channel,
    workspace_resolver: WorkspaceResolverCell | None,
    pool_name: str,
    emitter_factory: Callable | None,
    *,
    external_coding_deps: dict[str, Any] | None = None,
    observability_config: ObservabilityConfig | None = None,
) -> DefaultAgentFactory:
    if external_coding_deps is not None:
        factory: DefaultAgentFactory = ExternalCodingAwareFactory(
            default_llm_provider=provider,
            default_tool_manager=tool_manager,
            skill_manager=skill_manager,
            inbox_server=inbox_server,
            default_hooks=shared_hooks,
            default_hook_runner=shared_hook_runner,
            default_interceptor_chain=shared_interceptor_chain,
            control_channel=control_channel,
            external_coding_deps=external_coding_deps,
            observability_config=observability_config,
        )
    else:
        factory = DefaultAgentFactory(
            default_llm_provider=provider,
            default_tool_manager=tool_manager,
            skill_manager=skill_manager,
            inbox_server=inbox_server,
            default_hooks=shared_hooks,
            default_hook_runner=shared_hook_runner,
            default_interceptor_chain=shared_interceptor_chain,
            control_channel=control_channel,
            observability_config=observability_config,
        )

    # Wrap create_agent -> inject emitter for ALL agents (resident + subagent)
    # AND wire each pipeline's workspace_manager + pool_name so turns resolve
    # their per-turn stores from this workspace. ``workspace_resolver`` is the
    # late-binding cell build_resources fills with the PoolWorkspaceResources
    # (R) once the workspace is assembled; R.resolve_workspace().pool_data[pool]
    # is what the pipeline reads per turn.
    #
    # ``emitter_factory`` arrives pre-wrapped by ``create_pool`` so both
    # this wrapper and ``AgentMaterializeDeps.emitter_factory`` see the same
    # factory (external subagents bypass this wrapper).
    _orig_create = factory.create_agent

    async def _create_with_emitter(*args: Any, **kwargs: Any) -> Any:
        instance = await _orig_create(*args, **kwargs)
        if instance.pipeline is not None:
            turn_runner = instance.pipeline._turn_runner
            if emitter_factory is not None:
                # ADR-0025 D4: post-construction wiring targets the runner's
                # sub-objects directly. ReActTurnRunner delegates to its
                # TurnContextBuilder.emitter_factory setter; ExternalTurnRunner
                # stores it directly.
                turn_runner.set_emitter_factory(emitter_factory)
            if workspace_resolver is not None:
                turn_runner.set_pool_context(
                    workspace_manager=workspace_resolver, pool_name=pool_name
                )
        return instance

    factory.create_agent = _create_with_emitter  # type: ignore[method-assign]
    return factory


# ═══════════════════════════════════════════════════════════════════════════
# Agent pool
# ═══════════════════════════════════════════════════════════════════════════


def _build_agent_pool(
    broker,
    factory,
    context_manager,
    agent_bus,
    inbox_consumer,
    session_factory,
    safety,
    retention,
    pool_name: str,
    *,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
) -> AgentPool:
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        default_context_manager=context_manager,
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


# ═══════════════════════════════════════════════════════════════════════════
# Main agent registration
# ═══════════════════════════════════════════════════════════════════════════


async def _register_main_agent(
    pool: AgentPool,
    main_spec: MainAgentSpec,
    assembly_deps: PoolAssemblyDeps,
    system_prompt: str,
    safety: RuntimeSafetyPolicy,
    pool_name: str,
    *,
    factory: DefaultAgentFactory,
    broker: MessageBroker,
    context_manager: Any,
    bot_model_config: BotModelConfig | None,
) -> None:
    """Register the main (NORMAL) agent with factory defaults (Design B).

    The normal agent is a plain ``MainAgentSpec`` (inline in ``pool.yml``); its
    ``max_steps`` / ``tool_preset`` / ``tool_supplements`` / ``approval`` /
    ``use_terminal`` / ``terminal_visibility`` are read from ``main_spec``.
    """
    from modex_agent.multi_agent.descriptor import (
        AgentDescriptor,
        AgentLLMConfig,
    )

    resolved_cfg = _resolved_or_placeholder(bot_model_config)
    default_resolved = resolved_cfg.default_resolved()
    descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name=main_spec.agent_name),
        llm_config=AgentLLMConfig(
            model=default_resolved.model.model,
            temperature=default_resolved.model.temperature,
            max_output_tokens=default_resolved.model.max_output_tokens,
            reasoning_effort=default_resolved.model.reasoning_effort,
        ),
        system_prompt_template=system_prompt,
        max_iterations=main_spec.max_steps,
        execution_strategy=main_spec.execution_strategy,
        context_strategy="persistent",
        safety_policy=safety,
        comm_kind=AgentCommKind.NORMAL,
        memory_config=assembly_deps.memory,
        roles=list(main_spec.roles),
    )
    instance = await factory.create_agent(
        descriptor,
        broker=broker,
        tool_manager=None,
        skill_manager=None,
        context_manager=context_manager,
        hooks=[],
    )
    await pool.register_resident(descriptor, instance)
    logger.info(
        "Pool '%s': main agent '%s' registered (factory defaults)",
        pool_name, main_spec.agent_name,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Communication
# ═══════════════════════════════════════════════════════════════════════════


def _build_communication(
    pool: AgentPool,
    main_agent_name: str,
    broker,
    agent_bus,
    project_dir: Path,
    pool_name: str,
    templates: list,
    template_registry: AgentTemplateRegistry,
    *,
    session_registry: SessionRegistry | None = None,
    workspace_path_resolver: WorkspacePathResolver | None = None,
    trace_enabled: bool = True,
) -> tuple[AgentCommunicationService, CommunicationTargetStore]:
    """Build the slimmed AgentCommunicationService + target store.

    ADR-0015 D5: the service is a pure router - it no longer takes the ~30
    construction params it once did. ``AgentMaterializeDeps`` (built once in
    ``create_pool``) carries the subagent construction deps, injected into
    ``AgentPool`` for the Drainer-spawner. This function wires only the
    router + the target store.
    """
    main_address = AgentAddress(name=main_agent_name)
    main_service = AgentCommunicationService(
        source=main_address,
        broker=broker,
        registry=pool,
        agent_bus=agent_bus,
        template_registry=template_registry,
        pool=pool,
        pool_name=pool_name,
        project_dir=project_dir,
        session_registry=session_registry,
        workspace_path_resolver=workspace_path_resolver,
        trace_enabled=trace_enabled,
    )

    # Communication target store - populate from registered agents + templates
    main_store = CommunicationTargetStore()
    for p in pool.list_profiles():
        if p.name != main_agent_name:
            main_store.add(
                CommunicationTarget(
                    name=p.name,
                    kind=p.comm_kind,
                    description=p.role_description,
                )
            )
    for t in templates:
        main_store.add(
            CommunicationTarget(
                name=t.spec.agent_name,
                kind=AgentCommKind.SUBAGENT,
                description=t.spec.description,
                execution_strategy=t.spec.execution_strategy,
            )
        )
    logger.info("Pool '%s': communication store (%d targets)", pool_name, len(main_store.list()))
    return main_service, main_store


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline wiring
# ═══════════════════════════════════════════════════════════════════════════


class UserNoticeCleanupListener(MemoryCleanupListener):
    """Pushes transient English notices when session memory is compacted.

    Fires around the blocking archive-generation LLM call so the user
    understands the pause instead of seeing a stuck agent. Notices go through
    AgentNotificationService (tagged ``message_type=notice`` so the
    ChannelRouter fans them to the originating channel AND the WebUI observer)
    and are never written to session memory/history.
    """

    _START_NOTICE = "[compact] Consolidating conversation memory, please wait..."
    _DONE_NOTICE = "[compact] Memory consolidated."

    def __init__(self, notification_service: AgentNotificationService) -> None:
        self._svc = notification_service

    async def on_cleanup_triggered(
        self, context: MemoryContext, reason: CompressionReason
    ) -> None:
        session_id = context.session_id
        if session_id is None:
            return
        await self._svc.send_notice(session_id, self._START_NOTICE)

    async def on_cleanup_finished(
        self, context: MemoryContext, result: CleanupResult
    ) -> None:
        # ScopedMessageHistory only calls this when result.triggered.
        session_id = context.session_id
        if session_id is None:
            return
        await self._svc.send_notice(session_id, self._DONE_NOTICE)


def _wire_main_pipeline(
    pool: AgentPool,
    main_agent_name: str,
    inbox_consumer,
    notification_service,
    shared_interceptor_chain,
    im_ui,
    main_spec: MainAgentSpec,
    assembly_deps: PoolAssemblyDeps,
    project_dir: Path,
    command_processor,
    pool_name: str,
    tool_manager: ToolManager,
    *,
    root_provider: WorkspaceRootProvider | None = None,
    bot_model_config: BotModelConfig | None,
    model_choice_registry: ModelChoiceRegistry,
    cassette_recorder: CassetteRecorder | None = None,
) -> None:
    """Wire hooks, interceptors, governance, and command processor on main pipeline.

    The experience review hook and turn_store are NOT wired here - the review
    hook is built in bot.workspace.wiring._wire_pool_to_resources from
    the workspace's pool_data, and turn_store is resolved per turn from the
    workspace snapshot.

    ``root_provider`` is the per-workspace working-dir provider (the SAME one
    the file tools use). It anchors the approval classifier's ``./*`` patterns
    to the active workspace so in-workspace writes are auto-allowed; without it
    the classifier would fall back to ``project_dir`` (the bot project), gating
    every in-workspace write as DANGEROUS.
    """
    main_instance = pool._agents.get(main_agent_name)
    if main_instance is None or main_instance.pipeline is None:
        logger.warning(
            "Pool '%s': cannot wire pipeline - main_instance=%s",
            pool_name,
            type(main_instance).__name__ if main_instance else None,
        )
        return

    pipeline = main_instance.pipeline

    # Hooks
    # InboxFlushHook is NOT added here: the AgentFactory auto-injects it onto
    # pipeline.hook_runner for every agent (main + subagent) with
    # inbox_strategy != "none", so fold-in is wired in one place.
    _add_hook(pipeline, MaxIterationNotifyHook(notification_service=notification_service))
    _add_hook(pipeline, TurnOutcomeNotifyHook(notification_service=notification_service))
    # TodoCompletionProbeHook was previously wired here to force a todo_read
    # when the main agent tried to end a turn with unfinished todos. It is
    # deprecated: the correct approach is to rely on the system prompt layer
    # (TodoAwareSystemPromptProvider) and clear tool descriptions instead of
    # injecting synthetic tool calls into the conversation history.
    _add_hook(
        pipeline,
        ModelChoiceBindHook(
            _resolved_or_placeholder(bot_model_config),
            model_choice_registry,
        ),
    )
    if cassette_recorder is not None:
        _add_hook(pipeline, CassetteFlushHook(cassette_recorder))

    # ExternalTurnRunner has no builder/approval_renderer, so access them
    # through the ABC's typed read-only properties (None for external_coding).
    turn_runner = pipeline._turn_runner
    builder = turn_runner.turn_context_builder
    approval = turn_runner.approval_renderer
    if builder is not None:
        builder.interceptor_chain = shared_interceptor_chain
        builder.governance = create_governance(assembly_deps.memory)
    if approval is not None:
        approval.user_interface = im_ui

    from modex_agent.ioc.factories.approval import build_approval_runtime
    from modex_agent.runtime.services import AgentRuntimeServices

    approval_runtime = build_approval_runtime(
        main_spec.approval, project_root=project_dir, root_provider=root_provider
    )
    resolved_cfg = _resolved_or_placeholder(bot_model_config)
    default_resolved = resolved_cfg.default_resolved()
    services_kwargs: dict[str, Any] = dict(
        safety=pipeline.safety,
        model_capabilities=default_resolved.capabilities,
    )
    if approval_runtime is not None:
        services_kwargs["approval"] = approval_runtime
    if builder is not None:
        builder.runtime_services = AgentRuntimeServices(**services_kwargs)

    # Command processor (convention: use provided, else default)
    if command_processor is not None:
        pipeline.command_processor = command_processor
    else:
        from modex_agent.commands.processor import SlashCommandProcessor

        pipeline.command_processor = SlashCommandProcessor.default()

    logger.info(
        "Pool '%s': pipeline wired - cmd_processor=%s, skill_manager=%s",
        pool_name,
        type(pipeline.command_processor).__name__,
        type(pipeline.skill_manager).__name__ if pipeline.skill_manager else None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers (kept from original)
# ═══════════════════════════════════════════════════════════════════════════


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)
