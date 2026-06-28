"""Pool builder — IOC-style factory that builds one PoolInstance from PoolConfig.

Each build step is a focused method.  Convention over configuration:
config drives behaviour; methods read from PoolConfig / AgentConfig with
sensible defaults.  No giant if-else chains.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # ``WorkspaceHandle`` / ``WorkspaceResolverCell`` live in the bundle,
    # which is imported by BotService via this module; deferring them to
    # TYPE_CHECKING keeps the import graph acyclic. Runtime references
    # (``WorkspaceHandleRootProvider``) are imported lazily inside
    # ``create_pool`` for the same reason.
    from bot.workspace.handle import (
        WorkspaceHandle,
        WorkspaceResolverCell,
    )
    from modex_agent.memory.cleanup import CleanupResult
    from modex_agent.memory.core.models import CompressionReason

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.builtin import InboxFlushHook
from modex_agent.hook.notification import (
    AgentNotificationService,
    MaxIterationNotifyHook,
    TurnOutcomeNotifyHook,
)
from modex_agent.ioc.configs.agent import AgentConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.pool import PoolConfig
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.core.scope import MemoryContext
from modex_agent.memory.cleanup_events import MemoryCleanupListener
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.messaging.broker_bridge import BrokerBridgeService, OutputRoute
from modex_agent.multi_agent import (
    AgentDescriptor,
    AgentPool,
    DefaultAgentFactory,
    SessionRetentionPolicy,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_tracker import CommunicationTracker
from modex_agent.multi_agent.bus import AgentMessageBus
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication import AgentCommunicationService
from modex_agent.multi_agent.descriptor import AgentLLMConfig
from modex_agent.multi_agent.inbox.consumer import InboxConsumer
from modex_agent.multi_agent.inbox.server import InboxServer
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    SendToAgentTool,
)
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.tools.standard import FindFilesTool, SearchFilesTool
from modex_agent.tools.terminal import SubprocessExecutor, SubprocessTool
from modex_agent.tools.terminal.backends.factory import (
    UnsupportedVisibilityForTransport,
    create_pty_backend,
)
from modex_agent.tools.terminal.managers import create_terminal_manager
from modex_agent.tools.terminal.types import TerminalVisibility, detect_platform_shell
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    wrap_standard_tools,
)

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
    # ── Injection points for bot-layer customization ──
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
) -> PoolInstance:
    """Build one PoolInstance's DEPLOYMENT resources from PoolConfig.

    Per-pool data (memory / runtime stores / experience layer) is owned by
    the workspace and passed in as the already-built ``pool_data`` snapshot;
    this factory wires only: provider, tool/skill/MCP/terminal managers, agent
    pool, broker bridge, and the communication service. ``workspace_handle``
    is the FIXED per-workspace target/data-root used to scope file/shell tools
    to this workspace (None = legacy/non-workspace path, e.g. unit tests).
    """
    main_cfg = _require_main_agent(pool_cfg)
    main_agent_name = main_cfg.name
    system_prompt = resolve_system_prompt(main_cfg, project_dir)

    provider = _build_llm_provider(pool_cfg, pool_name)
    terminal_manager = _build_terminal_manager(pool_cfg, pool_name, workspace_handle)

    # Per-pool data (memory/runtime/experience) is owned by the workspace and
    # passed in as an already-built snapshot. None = non-workspace wiring
    # (unit tests) — the fallback context manager keeps create_pool callable.
    context_manager = (
        pool_data.context_manager
        if pool_data is not None
        else _fallback_context_manager(main_cfg, system_prompt)
    )
    if pool_data is not None:
        await ensure_long_term_defaults(
            project_dir, pool_cfg.memory, pool_data.context_manager.memory_system
        )

    root_provider: WorkspaceRootProvider | None = None
    if workspace_handle is not None:
        # Lazy import: the bundle imports BotService (via this module's
        # package), so a top-level import would create a cycle.
        from bot.workspace.handle import WorkspaceHandleRootProvider

        root_provider = WorkspaceHandleRootProvider(workspace_handle)
    tool_manager, mcp_manager = await _build_tools(
        pool_cfg, main_cfg, terminal_manager, project_dir,
        output_adapter, pool_name, data_dir, pool_data, root_provider,
    )
    _register_extra_tools_from_config(tool_manager, main_cfg, pool_name)

    skill_manager = _build_skill_manager(main_cfg, project_dir, pool_name)
    factory = _build_agent_factory(
        provider, tool_manager, skill_manager,
        inbox_server, shared_hooks, shared_hook_runner,
        shared_interceptor_chain, control_channel,
        workspace_resolver, pool_name, emitter_factory,
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

    # Register a compaction listener that notifies the user when session memory
    # is being consolidated (the blocking archive LLM call otherwise looks like
    # a stuck agent). Only the workspace-backed (DefaultMemorySystem) path.
    if pool_data is not None:
        memory_system = pool_data.context_manager.memory_system
        if memory_system is not None:
            memory_system.add_cleanup_listener(
                UserNoticeCleanupListener(notification_service)
            )
    main_service, main_store = _build_communication(
        pool, main_agent_name, broker, agent_bus,
        comm_tracker, project_dir, pool_name, pool_cfg,
        safety, inbox_consumer, notification_service,
        data_dir,
        # ── Injection points ──
        output_adapter_factory=output_adapter_factory,
        on_subagent_created=on_subagent_created,
        session_registry=session_registry,
        workspace_resolver=workspace_resolver,
        root_provider=root_provider,
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

    _wire_main_pipeline(
        pool, main_agent_name, inbox_consumer,
        notification_service,
        shared_interceptor_chain,
        im_ui, pool_cfg, project_dir,
        command_processor, pool_name,
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
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        mcp_manager=mcp_manager,
        terminal_manager=terminal_manager,
        main_agent_name=main_agent_name,
        provider=provider,
        notification_service=notification_service,
        communication_service=main_service,
    )


def _fallback_context_manager(main_cfg: AgentConfig, system_prompt: str) -> Any:
    """A minimal context_manager for tests / non-workspace wiring.

    The main agent's real context manager comes from the workspace pool_data;
    this fallback keeps create_pool callable without a workspace (used by
    unit tests that mock the build steps).
    """

    return MemorySystemContextManager(
        memory_system=None,
        default_agent_id=main_cfg.name,
        default_agent_role="main",
        base_system_prompt=system_prompt,
        injection_policy=FullInjectionPolicy(pruned_manager=None),
        experience_manager=None,
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


def _build_terminal_manager(
    pool_cfg: PoolConfig,
    pool_name: str,
    workspace_handle: WorkspaceHandle | None,
) -> Any | None:
    """Create terminal manager from pool config.

    ADR-0010 two-axis construction. The user-facing YAML fields ``use_terminal``
    (bool) and ``terminal_visibility`` (bool) keep their semantics; the framework
    translates ``True`` → ``TerminalVisibility.VISIBLE`` and ``False`` →
    ``TerminalVisibility.HIDDEN`` and constructs the manager via the two-axis
    ``create_terminal_manager(shell_family=..., visibility=...)`` signature.

    Fallback chain: if the requested VISIBLE backend cannot be created on this
    platform (``UnsupportedVisibilityForTransport``), retry with HIDDEN. If HIDDEN
    also fails, fall back to SubprocessTool-only (return None) so the agent still
    works. The shell family is auto-detected via ``detect_platform_shell``.

    ADR-0010 Consequences: the degradation decision (VISIBLE → HIDDEN) belongs
    HERE at pool-build time, not on the first command. ``create_terminal_manager``
    stores a LAZY backend factory that only instantiates the backend when a
    session is first created, so we probe ``create_pty_backend(visibility=...)``
    eagerly to surface an unsupported (transport, visibility) combo now.
    """
    use_terminal = any(getattr(a, "use_terminal", False) for a in pool_cfg.agents)
    if not use_terminal:
        logger.info("Pool '%s': use_terminal=false, skipping terminal tools", pool_name)
        return None

    visibility_bool: bool = True
    for a in pool_cfg.agents:
        if getattr(a, "role", None) == "main":
            visibility_bool = getattr(a, "terminal_visibility", True)
            break

    shell_info = detect_platform_shell()
    if shell_info is None:
        logger.warning(
            "Pool '%s': no supported shell detected; falling back to SubprocessTool.",
            pool_name,
        )
        return None
    shell_family = shell_info.family

    default_cwd: str | None = (
        str(workspace_handle.current) if workspace_handle is not None else None
    )

    attempts: list[TerminalVisibility] = (
        [TerminalVisibility.VISIBLE, TerminalVisibility.HIDDEN]
        if visibility_bool
        else [TerminalVisibility.HIDDEN]
    )

    last_err: Exception | None = None
    for vis in attempts:
        try:
            # create_terminal_manager stores a LAZY backend factory, so probe the
            # backend now to surface an unsupported (transport, visibility) combo
            # at pool-build time (ADR-0010: degradation decision belongs here,
            # not on the first command).
            create_pty_backend(visibility=vis)
            mgr = create_terminal_manager(
                shell_family=shell_family,
                visibility=vis,
                default_cwd=default_cwd,
            )
            logger.info(
                "Pool '%s': terminal manager created (family=%s, visibility=%s)",
                pool_name,
                shell_family.value,
                vis.value,
            )
            return mgr
        except UnsupportedVisibilityForTransport as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': terminal backend (family=%s, visibility=%s) unavailable: %s",
                pool_name,
                shell_family.value,
                vis.value,
                exc,
            )
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': terminal backend (family=%s, visibility=%s) failed: %s",
                pool_name,
                shell_family.value,
                vis.value,
                exc,
            )

    logger.error(
        "Pool '%s': ALL terminal backends failed (tried %s). Last error: %s. "
        "Falling back to SubprocessTool only.",
        pool_name,
        attempts,
        last_err,
    )
    return None


# ── Memory ───────────────────────────────────────────────────────────────


# NOTE: memory / runtime stores / experience layer are no longer built here.
# They are owned by the active Workspace (Workspace.build_pool_data) and
# resolved at turn time via the per-turn PoolData snapshot. Only the
# long-term-defaults helper remains, invoked from create_pool against the
# workspace-provided memory_system.


async def ensure_long_term_defaults(
    project_dir: Path,
    memory_cfg: MemoryConfig | None,
    memory_system: DefaultMemorySystem,
) -> None:
    """Initialize default long-term memory files if knowledge is enabled.

    Supports both old ``long_term`` config (deprecated) and new ``knowledge``
    config. Template paths in config are relative to the project directory.
    Resolves them to absolute paths before calling ``ensure_defaults`` so
    the knowledge layer finds templates regardless of CWD (critical after
    ``/cd`` switches the conversation to a different workspace).
    """
    if memory_cfg is None:
        return

    knowledge_enabled = False
    if memory_cfg.long_term is not None and memory_cfg.long_term.enabled:
        knowledge_enabled = True
    if memory_cfg.knowledge is not None and memory_cfg.knowledge.enabled:
        knowledge_enabled = True
    if not knowledge_enabled:
        return

    lt_mgr = memory_system.knowledge_manager
    if lt_mgr is None:
        return

    raw_template_dir: str | None = None
    if memory_cfg.knowledge is not None:
        raw_template_dir = memory_cfg.knowledge.default_templates_dir
    if not raw_template_dir and memory_cfg.long_term is not None:
        raw_template_dir = memory_cfg.long_term.default_templates_dir
    if raw_template_dir:
        abs_template_dir = str((project_dir / raw_template_dir).resolve())
        lt_mgr._config = _dc_replace(
            lt_mgr._config,
            default_templates_dir=abs_template_dir,
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


# ── Context ──────────────────────────────────────────────────────────────


# NOTE: _build_context was removed — the context manager is now built inside
# Workspace.build_pool_data and resolved from pool_data at turn time.


# ── Tools ────────────────────────────────────────────────────────────────


async def _build_tools(
    pool_cfg: PoolConfig,
    main_cfg: AgentConfig,
    terminal_manager,
    project_dir: Path,
    output_adapter,
    pool_name: str,
    data_dir: Path,
    pool_data: PoolDataSnapshot | None,
    root_provider: WorkspaceRootProvider | None,
) -> tuple[InMemoryToolManager, Any | None]:
    """Build tool manager from config — convention over configuration.

    When ``root_provider`` is given, the standard file/search/shell tools are
    wrapped via :func:`wrap_standard_tools` so their relative paths resolve
    against THIS workspace's root (a workspace switch is a different workspace
    with its own provider). Terminal tools (Command/Process/Terminal) stay
    UNWRAPPED — their cwd is the terminal manager's, bound separately.
    """
    tm = InMemoryToolManager(config=ToolManagerConfig())

    # File tools (always registered). Wrap when a workspace root provider is
    # wired so relative paths resolve against the workspace, not process CWD.
    file_tools = _make_file_tools()
    if root_provider is not None:
        file_tools = wrap_standard_tools(file_tools, root_provider)
    for tool in file_tools:
        tm.register(tool)

    # Terminal tools — or subprocess fallback
    if terminal_manager is not None:
        from modex_agent.tools.terminal import CommandTool, ProcessRegistry, ProcessTool, TerminalTool
        from modex_agent.tools.terminal.config import TerminalRuntimeConfig

        cfg = TerminalRuntimeConfig()
        registry = ProcessRegistry(config=cfg)
        tm.register(CommandTool(manager=terminal_manager, registry=registry, config=cfg))
        tm.register(ProcessTool(registry=registry, manager=terminal_manager))
        tm.register(TerminalTool(terminal_manager))
        logger.info("Pool '%s': terminal tools registered (Command/Process/Terminal)", pool_name)
    else:
        # SubprocessTool is workspace-scoped when a provider is wired (its
        # working_dir defaults to the workspace root); legacy unwrapped path
        # otherwise (tests / non-workspace wiring).
        sub = SubprocessTool(executor=SubprocessExecutor(), timeout=300)
        if root_provider is not None:
            sub_tools = wrap_standard_tools([sub], root_provider)
            tm.register(sub_tools[0])
        else:
            tm.register(sub)
        logger.info("Pool '%s': SubprocessTool registered (no terminal backend)", pool_name)

    # Search tools (always registered); wrap to scope their path arg.
    search_tools = [SearchFilesTool(), FindFilesTool()]
    if root_provider is not None:
        search_tools = wrap_standard_tools(search_tools, root_provider)
    for tool in search_tools:
        tm.register(tool)

    # Custom tools
    from bot.tools.custom import SendFileToUserTool

    tm.register(SendFileToUserTool(output_adapter=output_adapter))

    # Experience tool (if enabled in config). The experience dir comes from
    # the workspace's pool_data (fixed per workspace); fallback to a data_dir
    # relative path for the non-workspace (test) wiring.
    exp_cfg = getattr(main_cfg, "experience", None)
    if exp_cfg is not None and getattr(exp_cfg, "enabled", False):
        from modex_agent.core.experience import PerFileExperienceMetaStore
        from modex_agent.memory.tools.experience import ExperienceTool

        if pool_data is not None:
            base_exp_dir: Path = pool_data.experience_dir
            _exp_path: Callable[[], Path] = lambda: base_exp_dir
        else:
            fallback = data_dir / "experiences" / pool_name / main_cfg.name

            def _exp_path() -> Path:
                return fallback

        _exp_path().mkdir(parents=True, exist_ok=True)
        exp_meta = PerFileExperienceMetaStore(_exp_path)
        tm.register(ExperienceTool(_exp_path, exp_meta))
        logger.info("Pool '%s': experience tool registered", pool_name)

    # Todo tools — path from pool_data (pool-aware) or data_dir fallback,
    # mirroring the experience-tool path resolution above.
    from modex_agent.runtime.store import JsonFileTodoStore
    from modex_agent.tools.standard import TodoReadTool, TodoWriteTool

    if pool_data is not None and pool_data.runtime_dir is not None:
        todo_dir: Path = pool_data.runtime_dir / "todos"
    else:
        todo_dir = data_dir / "runtime_state" / pool_name / "todos"
    todo_store = JsonFileTodoStore(todo_dir)
    tm.register(TodoWriteTool(todo_store))
    tm.register(TodoReadTool(todo_store))
    logger.info("Pool '%s': todo tools registered (dir=%s)", pool_name, todo_dir)

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


# NOTE: _build_runtime_stores was removed — runtime stores (turn/command/trace)
# are now built inside Workspace.build_pool_data and resolved per turn from the
# PoolData snapshot. The agent factory no longer takes a turn_store / trace_store;
# the pipeline resolves them from the workspace snapshot.


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

    from modex_agent.core.skills import (
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


def _cell_sessions_dir(cell: WorkspaceResolverCell | None) -> Path | None:
    """Resolve the workspace sessions dir from a resolver cell.

    Returns ``None`` when the cell is not yet materialized so callers fall back
    to the ctxvar-based resolution path.
    """
    if cell is None:
        return None
    try:
        return cell.resolve_workspace().ctx.paths.sessions_dir
    except RuntimeError:
        return None


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
        # public setter — CompositeEmitter forwards to its children, so the
        # provider reaches every WebBotEmitter leaf.
        setter = getattr(emitter, "set_sessions_dir_provider", None)
        if setter is not None:
            setter(self._provider)
        return emitter


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
) -> DefaultAgentFactory:
    # turn_store / trace_store are intentionally NOT passed: the pipeline
    # resolves them per turn from the active workspace's PoolData snapshot.
    factory = DefaultAgentFactory(
        default_llm_provider=provider,
        default_tool_manager=tool_manager,
        skill_manager=skill_manager,
        inbox_server=inbox_server,
        default_hooks=shared_hooks,
        default_hook_runner=shared_hook_runner,
        default_interceptor_chain=shared_interceptor_chain,
        control_channel=control_channel,
    )

    # Wrap create_agent → inject emitter for ALL agents (resident + subagent)
    # AND wire each pipeline's workspace_manager + pool_name so turns resolve
    # their per-turn stores from this workspace. ``workspace_resolver`` is the
    # late-binding cell build_resources fills with the PoolWorkspaceResources
    # (R) once the workspace is assembled; R.resolve_workspace().pool_data[pool]
    # is what the pipeline reads per turn.
    #
    # When both emitters and a workspace resolver are configured, the emitter
    # factory is also wrapped so every created emitter gets a sessions-dir
    # provider derived from the resolver cell: transcript writes then resolve
    # the owning workspace's sessions dir from the cell — the SAME source that
    # memory/runtime/output use — instead of the fallible bind_workspace_root
    # ctxvar (which is lost across the broker-queue task boundary).
    _orig_create = factory.create_agent

    if emitter_factory is not None and workspace_resolver is not None:
        emitter_factory = _WorkspaceEmitterFactory(
            emitter_factory,
            lambda: _cell_sessions_dir(workspace_resolver),
        )

    async def _create_with_emitter(*args: Any, **kwargs: Any) -> Any:
        instance = await _orig_create(*args, **kwargs)
        if instance.pipeline is not None:
            if emitter_factory is not None:
                instance.pipeline.emitter_factory = emitter_factory
            if workspace_resolver is not None:
                instance.pipeline.workspace_manager = workspace_resolver
                instance.pipeline.pool_name = pool_name
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
    session_registry: SessionRegistry | None = None,
    session_store: SessionStore | None = None,
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
    safety,
    inbox_consumer,
    notification_service,
    data_dir: Path,
    # ── Injection points for bot-layer customization ──
    output_adapter_factory: Callable[[], OutputAdapter] | None = None,
    on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
    session_registry: SessionRegistry | None = None,
    workspace_resolver: WorkspaceResolverCell | None = None,
    root_provider: WorkspaceRootProvider | None = None,
):
    from modex_agent.multi_agent.template_registry import AgentTemplateRegistry

    template_registry = AgentTemplateRegistry(project_dir)
    templates = template_registry.list_templates(pool_name)
    logger.info("Pool '%s': %d subagent templates available", pool_name, len(templates))

    main_address = AgentAddress(name=main_agent_name)
    # memory_dir / runtime_dir / pruned_manager are intentionally NOT passed
    # as fixed values: AgentCommunicationService resolves them per call from
    # the active workspace's pool_data (see _resolve_pool_data). The fixed
    # ctor args are left None so the workspace path always wins when a
    # workspace_manager is wired.
    # However, we provide a stable fallback runtime_dir derived from data_dir
    # so subagents still get OUTPUT.md write tooling if pool_data resolution
    # is momentarily unavailable (e.g. during early boot or workspace switch).
    fallback_runtime_dir = data_dir / "runtime_state" / pool_name
    fallback_runtime_dir.mkdir(parents=True, exist_ok=True)
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
        safety=safety,
        pool_llm_model=pool_cfg.llm.model,
        pool_llm_temperature=pool_cfg.llm.temperature,
        pool_llm_max_tokens=pool_cfg.llm.max_tokens,
        inbox_consumer=inbox_consumer,
        notification_service=notification_service,
        main_agent_name=main_agent_name,
        runtime_dir=fallback_runtime_dir,
        # ── Injection points ──
        output_adapter_factory=output_adapter_factory,
        on_subagent_created=on_subagent_created,
        session_registry=session_registry,
        workspace_manager=workspace_resolver,
        root_provider=root_provider,
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


# NOTE: _build_experience_layer was removed. The experience review hook is
# now built in bot.workspace.wiring._wire_pool_to_resources from the
# workspace's pool_data, and the curator is workspace-scoped (Unit G). The
# review hook reads its dir from the per-turn workspace snapshot.


# ── Pipeline wiring ──────────────────────────────────────────────────────


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
    pool_cfg: PoolConfig,
    project_dir: Path,
    command_processor,
    pool_name: str,
) -> None:
    """Wire hooks, interceptors, governance, and command processor on main pipeline.

    The experience review hook and turn_store are NOT wired here — the review
    hook is built in bot.workspace.wiring._wire_pool_to_resources from
    the workspace's pool_data, and turn_store is resolved per turn from the
    workspace snapshot.
    """
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
    _add_hook(pipeline, TurnOutcomeNotifyHook(notification_service=notification_service))

    # Runtime wiring
    pipeline.interceptor_chain = shared_interceptor_chain
    pipeline._user_interface = im_ui
    pipeline.governance = create_governance(pool_cfg.memory, pool_cfg.llm.max_tokens)

    # Approval runtime — main agent only (subagents never pass through this
    # function). Opt-in: build_approval_runtime returns None when disabled or
    # no tools gated, leaving runtime_services untouched (default-off).
    from modex_agent.ioc.factories.approval import build_approval_runtime
    from modex_agent.runtime.services import AgentRuntimeServices

    main_cfg = next(a for a in pool_cfg.agents if a.role == "main")
    approval_runtime = build_approval_runtime(main_cfg.approval, project_root=project_dir)
    if approval_runtime is not None:
        # Sparse services: hooks/interceptors/governance stay None and are
        # sourced per-field from the builder defaults at turn time (identical
        # to the pre-wiring path). safety is passed explicitly because
        # AgentRuntimeServices.safety has a default_factory that would
        # otherwise clobber the pipeline's configured policy.
        pipeline.runtime_services = AgentRuntimeServices(
            approval=approval_runtime,
            safety=pipeline.safety,
        )

    # Command processor (convention: use provided, else default)
    if command_processor is not None:
        pipeline.command_processor = command_processor
    else:
        from modex_agent.commands.processor import SlashCommandProcessor

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
    "ast_grep_search": ("modex_agent.tools.ast", "AstGrepSearchTool"),
    "ast_grep_replace": ("modex_agent.tools.ast", "AstGrepReplaceTool"),
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


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)
