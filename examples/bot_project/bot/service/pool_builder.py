"""Pool builder — IOC-style factory that builds one PoolInstance from PoolConfig.

Each build step is a focused method.  Convention over configuration:
config drives behaviour; methods read from PoolConfig / AgentConfig with
sensible defaults.  No giant if-else chains.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from framework.core.emitter import ContentEmitter
from framework.control.channel import InMemoryControlChannel
from framework.core.llm_struct import RuntimeSafetyPolicy
from framework.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from framework.hook import HookErrorPolicy, HookRunner, HookSpec
from framework.hook.builtin import InboxFlushHook
from framework.hook.notification import AgentNotificationService, MaxIterationNotifyHook
from framework.ioc.configs.agent import AgentConfig
from framework.ioc.configs.pool import PoolConfig
from framework.ioc.factories.governance import create_governance
from framework.ioc.factories.llm import create_llm_provider
from framework.ioc.factories.memory import create_memory
from framework.memory.injection import FullInjectionPolicy
from framework.memory.system import MemorySystemContextManager
from framework.messaging.broker_bridge import BrokerBridgeService, OutputRoute
from framework.multi_agent import (
    AgentAddress,
    AgentDescriptor,
    AgentPool,
    CommunicationTracker,
    DefaultAgentFactory,
    SessionRetentionPolicy,
)
from framework.multi_agent.bus import AgentMessageBus
from framework.multi_agent.comm_kind import AgentCommKind
from framework.multi_agent.communication import AgentCommunicationService
from framework.multi_agent.descriptor import AgentLLMConfig
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server import InboxServer
from framework.core.session_id import SessionIdFactory
from framework.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToAgentTool,
)
from framework.pipeline.adapters import OutputAdapter
from framework.tools.standard import FindFilesTool, SearchFilesTool
from framework.tools.terminal import SubprocessExecutor, SubprocessTool
from framework.tools.terminal.managers import TerminalManagerBase

from .builders import _load_agent_mcp_tools, _make_file_tools, resolve_system_prompt
from .pool_instance import PoolInstance

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════


async def create_pool(
    pool_name: str,
    pool_cfg: PoolConfig,
    *,
    project_dir: Path,
    data_dir: Path,
    broker: Any,
    inbox_server: InboxServer,
    inbox_consumer: InboxConsumer,
    agent_bus: AgentMessageBus,
    output_adapter: OutputAdapter,
    safety: RuntimeSafetyPolicy,
    retention: SessionRetentionPolicy,
    comm_tracker: CommunicationTracker,
    approval_workspace: Path,
    im_ui: Any,
    shared_hooks: list,
    shared_hook_runner: HookRunner,
    shared_interceptor_chain: Any,
    control_channel: InMemoryControlChannel | None = None,
    command_processor: Any = None,
    workspace_context: Any = None,
    emitter_factory: Callable[[str], ContentEmitter] | None = None,
    # ── Injection points for bot-layer customization ──
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
    session_registry: Any = None,
    session_store: Any = None,
) -> PoolInstance:
    """Build one PoolInstance from PoolConfig.

    Each step delegates to a focused method — no inline if-else chains.
    """
    main_cfg = _require_main_agent(pool_cfg)
    main_agent_name = main_cfg.name
    system_prompt = resolve_system_prompt(main_cfg, project_dir)

    provider = _build_llm_provider(pool_cfg, pool_name)
    terminal_manager = _build_terminal_manager(pool_cfg, pool_name)
    memory_system = await _build_memory(pool_cfg, provider, data_dir, pool_name)
    context_manager = _build_context(memory_system, main_cfg, system_prompt, data_dir, pool_name)
    tool_manager, mcp_manager = await _build_tools(
        pool_cfg, main_cfg, terminal_manager, project_dir,
        output_adapter, pool_name, data_dir, workspace_context,
    )
    _register_extra_tools_from_config(tool_manager, main_cfg, pool_name)

    runtime_data_dir = data_dir / "runtime_state" / pool_name
    turn_store, command_store = _build_runtime_stores(runtime_data_dir, pool_name)
    skill_manager = _build_skill_manager(main_cfg, project_dir, pool_name)
    factory = _build_agent_factory(
        provider, tool_manager, skill_manager,
        inbox_server, shared_hooks, shared_hook_runner,
        shared_interceptor_chain, turn_store, control_channel,
        runtime_data_dir, emitter_factory,
    )
    session_factory = SessionIdFactory()
    pool = _build_agent_pool(
        broker, factory, context_manager, agent_bus,
        inbox_consumer, session_factory, safety, retention, comm_tracker,
        pool_name,
        session_registry=session_registry,
        session_store=session_store,
    )

    await _register_main_agent(pool, main_cfg, pool_cfg, system_prompt, safety, pool_name)

    notification_service = AgentNotificationService(
        output_adapter=output_adapter,
        agent_bus=agent_bus,
        parent_agent_name=main_agent_name,
    )
    main_service, main_store = _build_communication(
        pool, main_agent_name, broker, agent_bus,
        comm_tracker, project_dir, pool_name, pool_cfg, memory_system,
        safety, inbox_consumer, notification_service, data_dir, runtime_data_dir,
        # ── Injection points ──
        output_adapter_factory=output_adapter_factory,
        on_subagent_created=on_subagent_created,
        session_registry=session_registry,
    )
    tool_manager.register(
        SendToAgentTool(
            store=main_store,
            source=AgentAddress(name=main_agent_name),
            broker=broker,
            registry=pool,
            agent_bus=agent_bus,
            service=main_service,
            comm_tracker=comm_tracker,
        )
    )
    main_service._target_store = main_store
    logger.info("Pool '%s': communication tool registered", pool_name)

    exp_review_hook, exp_curator, exp_dir_ref = _build_experience_layer(
        main_cfg, provider, data_dir, pool_name
    )
    _wire_main_pipeline(
        pool, main_agent_name, inbox_consumer,
        notification_service, exp_review_hook,
        shared_interceptor_chain, turn_store,
        approval_workspace, im_ui, pool_cfg,
        command_processor, skill_manager,
        pool_name,
    )

    bridge = BrokerBridgeService(
        broker=broker,
        input_bindings={},
        output_routes=[
            OutputRoute(
                adapter=output_adapter,
                match_topic=f"agent:{main_agent_name}:out",
            ),
        ],
    )

    return PoolInstance(
        name=pool_name,
        config=pool_cfg,
        pool=pool,
        broker_bridge=bridge,
        memory_system=memory_system,
        context_manager=context_manager,
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        terminal_manager=terminal_manager,
        main_agent_name=main_agent_name,
        provider=provider,
        notification_service=notification_service,
        communication_service=main_service,
        experience_curator=exp_curator,
        experience_curator_task=None,
        experience_dir_ref=exp_dir_ref,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Build steps (one method = one concern)
# ═══════════════════════════════════════════════════════════════════════════


def _require_main_agent(pool_cfg: PoolConfig) -> AgentConfig:
    for a in pool_cfg.agents:
        if a.role == "main":
            return a
    raise ValueError(f"Pool has no agent with role='main': {pool_cfg}")


def _build_llm_provider(pool_cfg: PoolConfig, pool_name: str):
    provider = create_llm_provider(pool_cfg.llm)
    logger.info("Pool '%s': LLM provider (%s)", pool_name, pool_cfg.llm.model)
    return provider


# ── Terminal ─────────────────────────────────────────────────────────────


def _build_terminal_manager(pool_cfg: PoolConfig, pool_name: str) -> Any | None:
    """Create terminal manager from pool config.

    Convention: read ``use_terminal`` + ``terminal_visibility`` from the
    main agent config.  If ``use_terminal`` is false or absent, terminal
    tools are skipped and SubprocessTool is used instead.
    """
    use_terminal = any(getattr(a, "use_terminal", False) for a in pool_cfg.agents)
    if not use_terminal:
        logger.info("Pool '%s': use_terminal=false, skipping terminal tools", pool_name)
        return None

    visibility: bool = True
    for a in pool_cfg.agents:
        if getattr(a, "role", None) == "main":
            visibility = getattr(a, "terminal_visibility", True)
            break

    import sys

    from framework.tools.terminal.managers import create_terminal_manager

    if sys.platform == "win32":
        kinds = ["windows_visible", "windows_hidden"] if visibility else ["windows_hidden"]
    else:
        kinds = ["linux"]

    last_err: Exception | None = None
    for kind in kinds:
        try:
            mgr = create_terminal_manager(manager_kind=kind)
            logger.info(
                "Pool '%s': TerminalManager created (kind=%s, visibility=%s)",
                pool_name, kind, mgr.visibility.value,
            )
            return mgr
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': TerminalManager kind=%s failed: %s", pool_name, kind, exc
            )

    logger.error(
        "Pool '%s': ALL terminal backends failed (tried %s). "
        "Last error: %s. Falling back to SubprocessTool only.",
        pool_name, kinds, last_err,
    )
    return None


# ── Memory ───────────────────────────────────────────────────────────────


async def _build_memory(
    pool_cfg: PoolConfig,
    provider,
    data_dir: Path,
    pool_name: str,
):
    memory_dir = data_dir / "memory" / pool_name
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_system = create_memory(pool_cfg.memory, provider, memory_dir)
    await memory_system.initialize()
    logger.info("Pool '%s': MemorySystem (%s)", pool_name, memory_dir)
    return memory_system


# ── Context ──────────────────────────────────────────────────────────────


def _build_context(
    memory_system,
    main_cfg: AgentConfig,
    system_prompt: str,
    data_dir: Path,
    pool_name: str,
) -> MemorySystemContextManager:
    from bot.service.pool_builder import _build_pool_experience_manager

    exp_manager = _build_pool_experience_manager(main_cfg, data_dir, pool_name)
    ctx = MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id=main_cfg.name,
        default_agent_role="main",
        base_system_prompt=system_prompt,
        injection_policy=FullInjectionPolicy(pruned_manager=memory_system.pruned_manager),
        experience_manager=exp_manager,
    )
    if exp_manager is not None:
        logger.info("Pool '%s': experience manager injected", pool_name)
    return ctx


# ── Tools ────────────────────────────────────────────────────────────────


async def _build_tools(
    pool_cfg: PoolConfig,
    main_cfg: AgentConfig,
    terminal_manager,
    project_dir: Path,
    output_adapter,
    pool_name: str,
    data_dir: Path,
    workspace_context,
) -> tuple[InMemoryToolManager, Any | None]:
    """Build tool manager from config — convention over configuration."""
    tm = InMemoryToolManager(config=ToolManagerConfig())

    # File tools (always registered)
    for tool in _make_file_tools():
        tm.register(tool)

    # Terminal tools — or subprocess fallback
    if terminal_manager is not None:
        from framework.tools.terminal import CommandTool, ProcessRegistry, ProcessTool, TerminalTool
        from framework.tools.terminal.config import TerminalRuntimeConfig

        cfg = TerminalRuntimeConfig()
        registry = ProcessRegistry(config=cfg)
        tm.register(CommandTool(manager=terminal_manager, registry=registry, config=cfg))
        tm.register(ProcessTool(registry=registry, manager=terminal_manager))
        tm.register(TerminalTool(terminal_manager))
        logger.info("Pool '%s': terminal tools registered (Command/Process/Terminal)", pool_name)
    else:
        tm.register(SubprocessTool(executor=SubprocessExecutor(), timeout=60))
        logger.info("Pool '%s': SubprocessTool registered (no terminal backend)", pool_name)

    # Search tools (always registered)
    for tool in [SearchFilesTool(), FindFilesTool()]:
        tm.register(tool)

    # Custom tools
    from bot.tools.custom import SendFileToUserTool

    tm.register(SendFileToUserTool(output_adapter=output_adapter))

    # Experience tool (if enabled in config)
    exp_cfg = getattr(main_cfg, "experience", None)
    if exp_cfg is not None and getattr(exp_cfg, "enabled", False):
        from framework.core.experience.meta import PerFileExperienceMetaStore
        from framework.memory.tools.experience import ExperienceTool

        if workspace_context is not None:
            def _exp_path() -> Path:
                return workspace_context.data_dir / "experiences" / pool_name / main_cfg.name
        else:
            def _exp_path() -> Path:
                return data_dir / "experiences" / pool_name / main_cfg.name

        _exp_path().mkdir(parents=True, exist_ok=True)
        exp_meta = PerFileExperienceMetaStore(_exp_path)
        tm.register(ExperienceTool(_exp_path, exp_meta))
        logger.info("Pool '%s': experience tool registered", pool_name)

    # MCP tools (convention: config/mcp/{agent_name}.json)
    # Respect pool-level mcp.enabled toggle and never let MCP failures break
    # the rest of the tool manager / pool creation.
    mcp_tools: list[Any] = []
    mcp_manager: Any | None = None
    if pool_cfg.mcp is not None and getattr(pool_cfg.mcp, "enabled", True):
        try:
            mcp_tools, mcp_manager = await _load_agent_mcp_tools(main_cfg.name, project_dir)
        except Exception as exc:
            logger.warning(
                "Pool '%s': MCP tool loading failed, skipping: %s", pool_name, exc
            )
    else:
        logger.info("Pool '%s': MCP disabled in pool config, skipping", pool_name)

    for tool in mcp_tools:
        tm.register(tool)
    if mcp_tools:
        logger.info("Pool '%s': %d MCP tools registered", pool_name, len(mcp_tools))

    logger.info("Pool '%s': ToolManager ready (%d tools total)", pool_name, len(tm.list_tools()))
    return tm, mcp_manager


# ── Extra tools ──────────────────────────────────────────────────────────


def _register_extra_tools_from_config(
    tool_manager: InMemoryToolManager,
    main_cfg: AgentConfig,
    pool_name: str,
) -> None:
    """Register AST/LSP tools declared in agent config (convention)."""
    extra_tools: list[str] = getattr(main_cfg, "extra_tools", []) or []
    if not extra_tools:
        return
    _register_extra_tools(tool_manager, extra_tools)
    logger.info("Pool '%s': extra_tools registered: %s", pool_name, extra_tools)


# ── Runtime stores ───────────────────────────────────────────────────────


def _build_runtime_stores(runtime_data_dir: Path, pool_name: str):
    from framework.agents.react.state import ReActRuntimeStateCodec
    from framework.runtime.codec import RuntimeStateCodecRegistry
    from framework.runtime.enums import AgentKind
    from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore

    codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    turn_store = JsonFileTurnStateStore(runtime_data_dir / "turns", codec_registry)
    command_store = JsonFileRuntimeCommandStore(runtime_data_dir / "commands")
    logger.info("Pool '%s': runtime stores (%s)", pool_name, runtime_data_dir)
    return turn_store, command_store


# ── Skill manager ────────────────────────────────────────────────────────


def _build_skill_manager(main_cfg: AgentConfig, project_dir: Path, pool_name: str):
    """Convention: skills/{pool_name}/{agent_name}/ — or explicit roots in config."""
    skill_roots = getattr(main_cfg, "skills", None)
    explicit_roots: list[str] = getattr(skill_roots, "roots", None) or []

    if explicit_roots:
        directories = [project_dir / r for r in explicit_roots]
    else:
        directories = [project_dir / "skills" / pool_name / main_cfg.name]

    logger.info(
        "Pool '%s': scanning skills: %s (exists=%s)",
        pool_name,
        [str(d) for d in directories],
        [d.exists() for d in directories],
    )
    found = [d for d in directories if d.resolve().exists()]
    if not found:
        logger.warning("Pool '%s': no skill directories found", pool_name)
        return None

    from framework.core.skills import (
        DefaultSkillBuilder,
        DirectorySkillCache,
        FileSkillSource,
        SkillManager,
    )

    source = FileSkillSource(
        directories=found, cache=True, layout="directory", skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(directories=found, layout="directory")
    builder = DefaultSkillBuilder(base_path=project_dir)
    mgr = SkillManager(source=source, builder=builder, cache=cache)
    return mgr


# ── Agent factory ────────────────────────────────────────────────────────


def _build_agent_factory(
    provider,
    tool_manager,
    skill_manager,
    inbox_server,
    shared_hooks,
    shared_hook_runner,
    shared_interceptor_chain,
    turn_store,
    control_channel,
    runtime_data_dir: Path,
    emitter_factory: Callable | None,
) -> DefaultAgentFactory:
    from framework.trace import JsonFileTraceStore

    trace_store = JsonFileTraceStore(base_dir=runtime_data_dir / "trace")
    factory = DefaultAgentFactory(
        default_llm_provider=provider,
        default_tool_manager=tool_manager,
        skill_manager=skill_manager,
        inbox_server=inbox_server,
        default_hooks=shared_hooks,
        default_hook_runner=shared_hook_runner,
        default_interceptor_chain=shared_interceptor_chain,
        default_turn_store=turn_store,
        control_channel=control_channel,
        trace_store=trace_store,
    )

    # Wrap create_agent → inject emitter for ALL agents (resident + subagent)
    _orig_create = factory.create_agent

    async def _create_with_emitter(*args: Any, **kwargs: Any) -> Any:
        instance = await _orig_create(*args, **kwargs)
        if emitter_factory is not None and instance.pipeline is not None:
            instance.pipeline.emitter_factory = emitter_factory
        return instance

    factory.create_agent = _create_with_emitter  # type: ignore[method-assign]
    return factory


# ── Agent pool ───────────────────────────────────────────────────────────


def _build_agent_pool(
    broker,
    factory,
    context_manager,
    agent_bus,
    inbox_consumer,
    session_factory,
    safety,
    retention,
    comm_tracker,
    pool_name: str,
    *,
    session_registry: Any = None,
    session_store: Any = None,
) -> AgentPool:
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        default_context_manager=context_manager,
        agent_bus=agent_bus,
        inbox_consumer=inbox_consumer,
        enable_inbox_polling=True,
        inbox_poll_interval=10.0,
        default_context_manager_factory=None,
        session_factory=session_factory,
        safety=safety,
        retention=retention,
        comm_tracker=comm_tracker,
        session_registry=session_registry,
        session_store=session_store,
    )
    logger.info("Pool '%s': AgentPool created", pool_name)
    return pool


# ── Main agent registration ──────────────────────────────────────────────


async def _register_main_agent(
    pool: AgentPool,
    main_cfg: AgentConfig,
    pool_cfg: PoolConfig,
    system_prompt: str,
    safety: RuntimeSafetyPolicy,
    pool_name: str,
) -> None:
    descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name=main_cfg.name),
        llm_config=AgentLLMConfig(
            model=pool_cfg.llm.model,
            temperature=pool_cfg.llm.temperature,
            max_tokens=pool_cfg.llm.max_tokens,
        ),
        system_prompt_template=system_prompt,
        context_strategy="persistent",
        max_iterations=main_cfg.max_steps,
        execution_strategy="react",
        safety_policy=safety,
    )
    await pool.register_resident(descriptor)
    logger.info("Pool '%s': main agent '%s' registered", pool_name, main_cfg.name)


# ── Communication ────────────────────────────────────────────────────────


def _build_communication(
    pool: AgentPool,
    main_agent_name: str,
    broker,
    agent_bus,
    comm_tracker,
    project_dir: Path,
    pool_name: str,
    pool_cfg: PoolConfig,
    memory_system,
    safety,
    inbox_consumer,
    notification_service,
    data_dir: Path,
    runtime_data_dir: Path,
    # ── Injection points for bot-layer customization ──
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
    session_registry: Any = None,
):
    from framework.multi_agent.template_registry import AgentTemplateRegistry

    template_registry = AgentTemplateRegistry(project_dir)
    templates = template_registry.list_templates(pool_name)
    logger.info("Pool '%s': %d subagent templates available", pool_name, len(templates))

    main_address = AgentAddress(name=main_agent_name)
    main_service = AgentCommunicationService(
        source=main_address,
        broker=broker,
        registry=pool,
        agent_bus=agent_bus,
        comm_tracker=comm_tracker,
        template_registry=template_registry,
        pool=pool,
        pool_name=pool_name,
        project_dir=project_dir,
        memory_dir=data_dir / "memory" / pool_name,
        safety=safety,
        pool_llm_model=pool_cfg.llm.model,
        pool_llm_temperature=pool_cfg.llm.temperature,
        pool_llm_max_tokens=pool_cfg.llm.max_tokens,
        inbox_consumer=inbox_consumer,
        notification_service=notification_service,
        main_agent_name=main_agent_name,
        pruned_manager=memory_system.pruned_manager,
        runtime_dir=runtime_data_dir,
        # ── Injection points ──
        output_adapter_factory=output_adapter_factory,
        on_subagent_created=on_subagent_created,
        session_registry=session_registry,
    )

    # Communication target store — populate from registered agents + templates
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
                name=t.agent_type,
                kind=AgentCommKind.SUBAGENT,
                description=t.description,
            )
        )
    logger.info("Pool '%s': communication store (%d targets)", pool_name, len(main_store.list()))
    return main_service, main_store


# ── Experience layer ─────────────────────────────────────────────────────


def _build_experience_layer(
    main_cfg: AgentConfig,
    provider,
    data_dir: Path,
    pool_name: str,
) -> tuple[Any | None, Any | None, list[Path] | None]:
    """Convention: if experience.enabled in agent config, wire review hook + curator.

    Returns ``(review_hook, curator, dir_ref)`` where *dir_ref* is a mutable
    single-element list holding the current experience directory.  All three
    returned objects use ``lambda: dir_ref[0]`` internally so they resolve the
    path dynamically — updating ``dir_ref[0]`` on workspace switch is all that
    is needed.
    """
    exp_cfg = getattr(main_cfg, "experience", None)
    if exp_cfg is None or not getattr(exp_cfg, "enabled", False):
        return None, None, None

    from framework.agents.experience.review_agent import ExperienceReviewAgent
    from framework.core.experience.curator import ExperienceCurator
    from framework.core.experience.meta import PerFileExperienceMetaStore
    from framework.hook.builtin.experience_review import ExperienceReviewHook

    exp_dir = data_dir / "experiences" / pool_name / main_cfg.name
    exp_dir.mkdir(parents=True, exist_ok=True)
    # Mutable reference so workspace switch can update all three objects at once.
    _dir_ref: list[Path] = [exp_dir]
    exp_meta = PerFileExperienceMetaStore(lambda: _dir_ref[0])

    review_agent = ExperienceReviewAgent(
        provider=provider,
        max_iterations=getattr(exp_cfg, "max_iterations", 50),
    )
    review_hook = ExperienceReviewHook(
        review_agent=review_agent,
        experience_dir=lambda: _dir_ref[0],
        meta_store=exp_meta,
        min_messages=getattr(exp_cfg, "min_messages", 6),
        exp_cooldown_turns=getattr(exp_cfg, "exp_cooldown_turns", 3),
    )
    logger.info("Pool '%s': ExperienceReviewHook created", pool_name)

    curator = ExperienceCurator(
        experience_dir=lambda: _dir_ref[0],
        meta_store=exp_meta,
        max_experiences=getattr(exp_cfg, "max_experiences", 20),
    )
    logger.info("Pool '%s': ExperienceCurator created", pool_name)
    return review_hook, curator, _dir_ref


# ── Pipeline wiring ──────────────────────────────────────────────────────


def _wire_main_pipeline(
    pool: AgentPool,
    main_agent_name: str,
    inbox_consumer,
    notification_service,
    exp_review_hook,
    shared_interceptor_chain,
    turn_store,
    approval_workspace,
    im_ui,
    pool_cfg: PoolConfig,
    command_processor,
    skill_manager,
    pool_name: str,
) -> None:
    """Wire hooks, interceptors, governance, and command processor on main pipeline."""
    main_instance = pool._agents.get(main_agent_name)
    if main_instance is None or main_instance.pipeline is None:
        logger.warning(
            "Pool '%s': cannot wire pipeline — main_instance=%s",
            pool_name,
            type(main_instance).__name__ if main_instance else None,
        )
        return

    pipeline = main_instance.pipeline

    # Hooks
    _add_hook(pipeline, InboxFlushHook(consumer=inbox_consumer, agent_name=main_agent_name))
    _add_hook(pipeline, MaxIterationNotifyHook(notification_service=notification_service))
    if exp_review_hook is not None:
        _add_hook(pipeline, exp_review_hook)

    # Runtime wiring
    pipeline.interceptor_chain = shared_interceptor_chain
    pipeline.turn_store = turn_store
    pipeline._approval_workspace = approval_workspace
    pipeline._user_interface = im_ui
    pipeline.governance = create_governance(pool_cfg.memory, pool_cfg.llm.max_tokens)

    # Command processor (convention: use provided, else default)
    if command_processor is not None:
        pipeline.command_processor = command_processor
    else:
        from framework.commands.processor import SlashCommandProcessor

        pipeline.command_processor = SlashCommandProcessor.default()

    logger.info(
        "Pool '%s': pipeline wired — cmd_processor=%s, skill_manager=%s",
        pool_name,
        type(pipeline.command_processor).__name__,
        type(pipeline.skill_manager).__name__ if pipeline.skill_manager else None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers (kept from original)
# ═══════════════════════════════════════════════════════════════════════════


_TOOL_REGISTRY: dict[str, tuple[str, str]] = {
    "ast_grep_search": ("framework.tools.ast", "AstGrepSearchTool"),
    "ast_grep_replace": ("framework.tools.ast", "AstGrepReplaceTool"),
}


def _register_extra_tools(tool_manager: InMemoryToolManager, tool_names: list[str]) -> None:
    import importlib

    for name in tool_names:
        entry = _TOOL_REGISTRY.get(name)
        if entry is None:
            logger.warning("Unknown extra_tool: %s", name)
            continue
        module_name, class_name = entry
        try:
            module = importlib.import_module(module_name)
            tool_cls = getattr(module, class_name)
            tool_manager.register(tool_cls())
        except Exception:
            logger.exception("Failed to register extra_tool: %s", name)


def _build_pool_experience_manager(
    main_cfg: AgentConfig,
    data_dir: Path,
    pool_name: str,
) -> Any | None:
    exp_cfg = getattr(main_cfg, "experience", None)
    if exp_cfg is None or not getattr(exp_cfg, "enabled", False):
        return None
    from framework.core.experience.manager import ExperienceManager
    from framework.core.experience.source import FileExperienceSource

    exp_dir = data_dir / "experiences" / pool_name / main_cfg.name
    exp_dir.mkdir(parents=True, exist_ok=True)
    return ExperienceManager(source=FileExperienceSource(directories=[exp_dir]))


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)
