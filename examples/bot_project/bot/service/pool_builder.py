"""create_pool() factory — builds one PoolInstance from PoolConfig."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

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
from framework.multi_agent.communication import AgentCommunicationService
from framework.multi_agent.descriptor import AgentLLMConfig
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server import InboxServer
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.tools import (
    ListCommunicationTargetsTool,
    SendToAgentTool,
)
from framework.pipeline.adapters import OutputAdapter
from framework.tools.standard import (
    EditFileTool,
    FindFilesTool,
    ListDirTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from framework.tools.terminal import SubprocessTool, SubprocessExecutor
from framework.tools.terminal.managers import TerminalManagerBase

from .builders import _load_agent_mcp_tools, _make_file_tools, resolve_system_prompt
from .pool_instance import PoolInstance

logger = logging.getLogger(__name__)


async def create_pool(
    pool_name: str,
    pool_cfg: PoolConfig,
    *,
    project_dir: Path,
    broker: Any,
    inbox_server: InboxServer,
    inbox_producer: InboxProducer,
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
) -> PoolInstance:

    main_cfg = next(a for a in pool_cfg.agents if a.role == "main")
    main_agent_name = main_cfg.name

    # 1. Per-pool LLM provider
    provider = create_llm_provider(pool_cfg.llm)
    logger.info("Pool '%s': LLM provider created (%s)", pool_name, pool_cfg.llm.model)

    # 2. Per-pool TerminalManager (lazy — sessions created on first use)
    terminal_manager = _create_terminal_manager(pool_cfg, project_dir)
    if terminal_manager is not None:
        logger.info("Pool '%s': TerminalManager created (%s, lazy)", pool_name, terminal_manager.visibility.value)

    # 3. Per-pool MemorySystem
    memory_dir = project_dir / "data" / "memory" / pool_name
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_system = create_memory(pool_cfg.memory, provider, memory_dir)
    await memory_system.initialize()
    logger.info("Pool '%s': MemorySystem initialized (%s)", pool_name, memory_dir)

    # 4. Per-pool ContextManager
    system_prompt = resolve_system_prompt(main_cfg, project_dir)
    context_manager = MemorySystemContextManager(
        memory_system=memory_system,
        default_agent_id=main_agent_name,
        default_agent_role="main",
        base_system_prompt=system_prompt,
        injection_policy=FullInjectionPolicy(),
    )

    # 5. Per-pool ToolManager (+ MCP)
    tool_manager, mcp_manager = await _build_pool_tool_manager(
        pool_cfg, main_cfg, terminal_manager, project_dir, output_adapter,
    )
    logger.info("Pool '%s': ToolManager ready (%d tools)", pool_name, len(tool_manager.list_tools()))

    # 5.1. Register extra_tools for main agent (AST, LSP, etc.)
    extra_tools = getattr(main_cfg, "extra_tools", []) or []
    if extra_tools:
        _register_extra_tools(tool_manager, extra_tools)
        logger.info("Pool '%s': %d extra_tools registered: %s", pool_name, len(extra_tools), extra_tools)

    # 5.5. Per-pool runtime stores (TurnStateStore + RuntimeCommandStore)
    from framework.agents.react.state import ReActRuntimeStateCodec
    from framework.runtime.codec import RuntimeStateCodecRegistry
    from framework.runtime.enums import AgentKind
    from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore
    runtime_data_dir = project_dir / "data" / "runtime_state" / pool_name
    codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
    turn_store = JsonFileTurnStateStore(runtime_data_dir / "turns", codec_registry)
    command_store = JsonFileRuntimeCommandStore(runtime_data_dir / "commands")
    logger.info("Pool '%s': runtime stores initialized (%s)", pool_name, runtime_data_dir)

    # 6. Per-pool SkillManager (convention: skills/{pool_name}/{agent_name}/)
    skill_manager = _build_pool_skill_manager(main_cfg, project_dir, pool_name)
    if skill_manager is not None:
        available = await skill_manager.list_skills()
        skill_names = [s.name for s in available]
        logger.info("Pool '%s': SkillManager ready (%d skills: %s)", pool_name, len(skill_names), skill_names)
    else:
        logger.warning("Pool '%s': SkillManager is None — skill directory not found, slash commands will not resolve", pool_name)

    # 7. AgentFactory (creates RuntimeContextManager internally for tool-call tracking)
    factory = DefaultAgentFactory(
        default_llm_provider=provider,
        default_tool_manager=tool_manager,
        skill_manager=skill_manager,
        inbox_server=inbox_server,
        default_hooks=shared_hooks,
        default_hook_runner=shared_hook_runner,
        default_interceptor_chain=shared_interceptor_chain,
        default_turn_store=turn_store,
    )

    # 8. AgentPool
    session_strategy = DefaultSessionIdStrategy(main_agent_name=main_agent_name)
    pool = AgentPool(
        broker=broker,
        agent_factory=factory,
        default_context_manager=context_manager,
        agent_bus=agent_bus,
        inbox_consumer=inbox_consumer,
        enable_inbox_polling=True,
        inbox_poll_interval=10.0,
        default_context_manager_factory=None,
        session_strategy=session_strategy,
        safety=safety,
        retention=retention,
        comm_tracker=comm_tracker,
    )
    logger.info("Pool '%s': AgentPool created", pool_name)

    # 9. Register main agent as resident
    main_descriptor = AgentDescriptor(
        address=AgentAddress(kind="agent", name=main_agent_name),
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
    await pool.register_resident(main_descriptor)
    logger.info("Pool '%s': main agent '%s' registered as resident", pool_name, main_agent_name)

    # 9.5 Register communication tools for the main agent

    # Load templates for communication target discovery
    from framework.multi_agent.template_registry import AgentTemplateRegistry
    template_registry = AgentTemplateRegistry(project_dir)
    templates = template_registry.list_templates(pool_name)
    logger.info("Pool '%s': %d templates available for dynamic creation", pool_name, len(templates))

    # 9.6 Per-pool notification service (must be before main_service for subagent hooks)
    notification_service = AgentNotificationService(
        output_adapter=output_adapter,
        agent_bus=agent_bus,
        session_strategy=session_strategy,
    )

    main_address = AgentAddress(name=main_agent_name)
    main_service = AgentCommunicationService(
        source=main_address, broker=broker, registry=pool,
        agent_bus=agent_bus, session_strategy=session_strategy,
        comm_tracker=comm_tracker,
        template_registry=template_registry,
        pool=pool,
        pool_name=pool_name,
        project_dir=project_dir,
        # Subagent creation dependencies
        memory_dir=memory_dir,
        safety=safety,
        pool_llm_model=pool_cfg.llm.model,
        pool_llm_temperature=pool_cfg.llm.temperature,
        pool_llm_max_tokens=pool_cfg.llm.max_tokens,
        inbox_consumer=inbox_consumer,
        notification_service=notification_service,
        main_agent_name=main_agent_name,
    )
    tool_manager.register(SendToAgentTool(
        source=main_address, broker=broker, registry=pool,
        agent_bus=agent_bus, service=main_service,
        comm_tracker=comm_tracker,
    ))
    tool_manager.register(ListCommunicationTargetsTool(
        self_address=main_address, registry=pool,
        template_registry=template_registry,
        pool_name=pool_name,
    ))
    logger.info("Pool '%s': communication tools registered for main agent", pool_name)

    # 10. Hooks
    max_iter_hook = MaxIterationNotifyHook(notification_service=notification_service)

    # Wire hooks on main agent's pipeline
    main_instance = pool._agents.get(main_agent_name)
    if main_instance is not None and main_instance.pipeline is not None:
        main_pipeline = main_instance.pipeline
        _add_hook(main_pipeline, InboxFlushHook(
            consumer=inbox_consumer, agent_name=main_agent_name,
        ))
        _add_hook(main_pipeline, max_iter_hook)

    # 12. Wire main agent runtime
    main_instance = pool._agents.get(main_agent_name)
    if main_instance is not None and main_instance.pipeline is not None:
        main_instance.pipeline.interceptor_chain = shared_interceptor_chain
        main_instance.pipeline.turn_store = turn_store
        main_instance.pipeline._approval_workspace = approval_workspace
        main_instance.pipeline._user_interface = im_ui
        main_instance.pipeline.governance = create_governance(
            pool_cfg.memory, pool_cfg.llm.max_tokens,
        )
        # Wire slash-command processor so /skill_name commands resolve
        # through SkillManager and are injected as context.
        from framework.commands.processor import SlashCommandProcessor
        main_instance.pipeline.command_processor = SlashCommandProcessor.default()
        logger.info(
            "Pool '%s': pipeline wired — command_processor=%s, skill_manager=%s",
            pool_name,
            type(main_instance.pipeline.command_processor).__name__,
            type(main_instance.pipeline.skill_manager).__name__ if main_instance.pipeline.skill_manager else None,
        )
    else:
        logger.warning(
            "Pool '%s': could not wire pipeline — main_instance=%s, pipeline=%s",
            pool_name,
            type(main_instance).__name__ if main_instance else None,
            type(main_instance.pipeline).__name__ if main_instance and main_instance.pipeline else None,
        )

    # 13. BrokerBridgeService (output routes only — input handled by PoolRouter)
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
    )


# ── internal helpers ──

def _register_extra_tools(tool_manager: InMemoryToolManager, tool_names: list[str]) -> None:
    """Register named tools by looking up their class in known modules.

    Supports: ast_grep_search, ast_grep_replace, lsp_diagnostics, lsp_navigation
    Falls back silently if a tool class cannot be imported.
    """
    _TOOL_REGISTRY: dict[str, tuple[str, str]] = {
        "ast_grep_search": ("framework.tools.ast", "AstGrepSearchTool"),
        "ast_grep_replace": ("framework.tools.ast", "AstGrepReplaceTool"),
        "lsp_diagnostics": ("framework.tools.lsp", "LspDiagnosticsTool"),
        "lsp_navigation": ("framework.tools.lsp", "LspNavigationTool"),
    }

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


def _create_terminal_manager(pool_cfg: PoolConfig, project_dir: Path) -> Any | None:
    """Create terminal manager with visibility-aware degradation chain.

    use_terminal=false → None (SubprocessTool only, no terminal tools).
    use_terminal=true:
      visibility="visible" → visible → hidden → None (subprocess fallback)
      visibility="hidden"  → hidden  → None (subprocess fallback)

    Linux/macOS has no visible backend; "visible" degrades to hidden (pexpect/tmux).
    """
    use_terminal = any(
        getattr(a, "use_terminal", False) for a in pool_cfg.agents
    )
    if not use_terminal:
        return None

    # Read visibility preference from the main agent
    visibility: bool = True
    for a in pool_cfg.agents:
        if getattr(a, "role", None) == "main":
            visibility = getattr(a, "terminal_visibility", True)
            break

    import sys
    from framework.tools.terminal.managers import create_terminal_manager

    if sys.platform == "win32":
        if visibility:
            kinds = ["windows_visible", "windows_hidden"]
        else:
            kinds = ["windows_hidden"]
    else:
        # Linux/macOS: no visible backend; "visible" degrades to hidden
        kinds = ["linux"]

    for kind in kinds:
        try:
            return create_terminal_manager(manager_kind=kind)
        except Exception:
            continue
    return None


async def _build_pool_tool_manager(
    pool_cfg: PoolConfig,
    main_cfg: AgentConfig,
    terminal_manager: TerminalManagerBase | None,
    project_dir: Path,
    output_adapter: OutputAdapter,
) -> tuple[InMemoryToolManager, Any | None]:
    tm = InMemoryToolManager(config=ToolManagerConfig(
        max_workers=10, enable_parallel=True, parallel_max_workers=5,
    ))
    for tool in _make_file_tools():
        tm.register(tool)

    if terminal_manager is not None:
        from framework.tools.terminal import CommandTool, ProcessTool, TerminalTool
        from framework.tools.terminal.config import TerminalRuntimeConfig
        from framework.tools.terminal import ProcessRegistry

        cfg = TerminalRuntimeConfig()
        registry = ProcessRegistry(config=cfg)

        tm.register(CommandTool(manager=terminal_manager, registry=registry, config=cfg))
        tm.register(ProcessTool(registry=registry, manager=terminal_manager))
        tm.register(TerminalTool(terminal_manager))
    else:
        shell_tool = SubprocessTool(executor=SubprocessExecutor(), timeout=60)
        tm.register(shell_tool)

    for tool in [SearchFilesTool(), FindFilesTool()]:
        tm.register(tool)

    from bot.tools.custom import SendFileToUserTool
    tm.register(SendFileToUserTool(output_adapter=output_adapter))

    # MCP: load per-agent config from config/mcp/{agentName}.json
    mcp_tools, mcp_manager = await _load_agent_mcp_tools(main_cfg.name, project_dir)
    for tool in mcp_tools:
        tm.register(tool)

    return tm, mcp_manager


def _build_pool_skill_manager(main_cfg: Any, project_dir: Path, pool_name: str) -> Any | None:
    # Default convention: skills/{pool_name}/{agent_name}/
    # Falls back to explicit skills.roots if configured in YAML
    skill_roots = getattr(main_cfg, "skills", None)
    explicit_roots: list[str] = getattr(skill_roots, "roots", None) or []

    if explicit_roots:
        directories = [project_dir / r for r in explicit_roots]
    else:
        # Convention: skills/{pool_name}/{agent_name}/
        directories = [project_dir / "skills" / pool_name / main_cfg.name]

    logger.info(
        "Pool '%s': scanning skill directories: %s (exists=%s)",
        pool_name,
        [str(d) for d in directories],
        [d.exists() for d in directories],
    )
    found = [d for d in directories if d.resolve().exists()]
    if not found:
        logger.warning("Pool '%s': no skill directories found, SkillManager will be None", pool_name)
        return None

    from framework.core.skills import (
        DirectorySkillCache,
        FileSkillSource,
        ProgressiveBuilder,
        SkillManager,
    )
    source = FileSkillSource(
        directories=found, cache=True, layout="directory",
        skill_filename="SKILL.md",
    )
    cache = DirectorySkillCache(directories=found, layout="directory")
    builder = ProgressiveBuilder(base_path=project_dir)
    return SkillManager(source=source, builder=builder, cache=cache)


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)
