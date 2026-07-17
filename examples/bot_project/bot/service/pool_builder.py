"""Pool builder — IOC-style factory that builds one PoolInstance from PoolSpec.

Each build step is a focused method.  Convention over configuration:
config drives behaviour; methods read from PoolSpec / MainAgentSpec with
sensible defaults.  No giant if-else chains.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import replace as _dc_replace
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
    from modex_agent.runtime.store import JsonFileTodoStore
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

from bot.config.memory_defaults import subagent_memory
from bot.service.model_choice import ModelChoiceBindHook, ModelChoiceRegistry
from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from bot.service.model_provider import BotModelProvider
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.constants import ExecutionStrategy
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.scope import MemoryContext
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolManager,
    ToolManagerConfig,
)
from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.notification import (
    AgentNotificationService,
    MaxIterationNotifyHook,
    TurnOutcomeNotifyHook,
)
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.observability import CassetteScope, ObservabilityConfig, TraceBackend
from modex_agent.ioc.factories.governance import create_governance
from modex_agent.memory.cleanup_events import MemoryCleanupListener
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.injection import FullInjectionPolicy
from modex_agent.memory.system import MemorySystemContextManager
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
from modex_agent.tools.terminal.backends.factory import (
    UnsupportedVisibilityForTransport,
)
from modex_agent.tools.terminal.managers import create_terminal_manager
from modex_agent.tools.terminal.types import TerminalVisibility, detect_platform_shell
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    wrap_standard_tools,
)
from modex_agent.trace.cassette import (
    CassetteFlushHook,
    CassetteRecorder,
    apply_cassette_wrapping,
)

from ._external_coding_wiring import (
    ExternalCodingAwareFactory,
    build_external_coding_deps,
    provider_executable_for,
    read_provider_kind,
)
from .builders import _load_agent_mcp_tools, build_inbox, build_todo_store, resolve_system_prompt

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
    # ── Injection points for bot-layer customization ──
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
) -> PoolInstance:
    """Build one PoolInstance's DEPLOYMENT resources from PoolSpec + deps.

    Per-pool data (memory / runtime stores / experience layer) is owned by
    the workspace and passed in as the already-built ``pool_data`` snapshot;
    this factory wires only: provider, tool/skill/MCP/terminal managers, agent
    pool, broker bridge, and the communication service. ``workspace_handle``
    is the FIXED per-workspace target/data-root used to scope file/shell tools
    to this workspace (None = legacy/non-workspace path, e.g. unit tests).
    """
    main_spec = pool_spec.main
    main_agent_name = main_spec.agent_name
    system_prompt = resolve_system_prompt(main_agent_name, project_dir)

    provider = _build_llm_provider(pool_name, bot_model_config)
    terminal_manager = _build_terminal_manager(main_spec, pool_name, workspace_handle)
    default_resolved = _resolved_or_placeholder(bot_model_config).default_resolved()

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
    agent_bus = LocalAgentMessageBus(
        producer=inbox_producer, consumer=inbox_consumer, broker=broker
    )

    # Per-pool data (memory/runtime/experience) is owned by the workspace and
    # passed in as an already-built snapshot. None = non-workspace wiring
    # (unit tests) — the fallback context manager keeps create_pool callable.
    context_manager = (
        pool_data.context_manager
        if pool_data is not None
        else _fallback_context_manager(main_spec, system_prompt)
    )
    if pool_data is not None:
        await ensure_long_term_defaults(
            project_dir, assembly_deps.memory, pool_data.context_manager.memory_system
        )

    root_provider: WorkspaceRootProvider | None = None
    if workspace_handle is not None:
        # Lazy import: the bundle imports BotService (via this module's
        # package), so a top-level import would create a cycle.
        from bot.workspace.handle import WorkspaceHandleRootProvider

        root_provider = WorkspaceHandleRootProvider(workspace_handle)
    # sessions_dir provider for transcript-writing tools (SendFileToUserTool):
    # derived from the resolver cell so the outbound record lands in the owning
    # workspace's transcript, mirroring the emitter factory wrapper. None when
    # no workspace resolver is wired (tests / legacy) — the store then falls
    # back to the bound ctxvar root.
    sessions_dir_provider: Callable[[], Path | None] | None = None
    if workspace_resolver is not None:
        sessions_dir_provider = lambda: _cell_sessions_dir(workspace_resolver)
    tool_manager, mcp_manager, todo_store = await _build_tools(
        main_spec, assembly_deps, terminal_manager, project_dir,
        output_adapter, pool_name, data_dir, pool_data, root_provider,
        transcript_store=transcript_store,
        sessions_dir_provider=sessions_dir_provider,
        mcp_registry=mcp_registry,
        persistence=persistence,
        app_config=app_config,
    )

    cassette_enabled, cassette_scope, cassette_base_dir = _resolve_cassette_config(
        app_config, data_dir
    )
    provider, tool_manager, cassette_recorder = apply_cassette_wrapping(
        provider,
        tool_manager,
        cassette_enabled=cassette_enabled,
        cassette_scope=cassette_scope,
        base_dir=cassette_base_dir,
    )

    skill_manager = _build_skill_manager(main_agent_name, project_dir, pool_name)

    external_coding_deps: dict[str, Any] | None = None
    provider_available = True
    if main_spec.execution_strategy == ExecutionStrategy.EXTERNAL_CODING:
        provider_kind = read_provider_kind(pool_spec, project_dir)
        executable = provider_executable_for(provider_kind)
        if shutil.which(executable) is None:
            logger.warning(
                "Pool '%s': external_coding provider %r not found on PATH; "
                "skipping pool registration",
                pool_name,
                executable,
            )
            provider_available = False
        else:
            workspace_dir = (
                workspace_handle.current if workspace_handle is not None else project_dir
            )
            external_coding_deps = build_external_coding_deps(
                pool_name=pool_name,
                pool_spec=pool_spec,
                project_dir=project_dir,
                inbox_dir=inbox_dir,
                workspace_dir=workspace_dir,
                main_agent_name=main_agent_name,
                base_env=dict(os.environ),
                app_config=app_config,
                persistence=persistence,
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
    pool = _build_agent_pool(
        broker, factory, context_manager, agent_bus,
        inbox_consumer, session_factory, safety, retention,
        pool_name,
        session_registry=session_registry,
        session_store=session_store,
    )

    # ── Materialize deps + template registry (built once, injected into pool) ──
    # ADR-0015 D5: the deps bundle carries subagent construction params; the
    # ── Materialize deps + template registry (built once, injected into pool) ──
    # ADR-0015 D5: the deps bundle carries subagent construction params; the
    # template registry holds the YAML-defined SUBAGENT templates (the normal
    # agent is the main-agent MainAgentSpec inline in pool.yml, NOT a template). Both
    # are constructed here (before _register_main_agent) so the lazy
    # InboxPoller-spawner shares the same bundle.
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
        agent_bus=agent_bus,
        parent_agent_name=main_agent_name,
    )

    deps = AgentMaterializeDeps(
        agent_factory=factory,
        pool=pool,
        session_factory=session_factory,
        broker=broker,
        safety=safety,
        llm_model=default_resolved.model.model,
        llm_temperature=default_resolved.model.temperature,
        llm_max_output_tokens=default_resolved.model.max_output_tokens,
        llm_reasoning_effort=default_resolved.model.reasoning_effort,
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
    )
    pool._materialize_deps = deps
    pool._template_registry = template_registry
    pool._pool_name = pool_name
    pool._context_fork_builder = context_fork_builder

    # Task 7: build + attach + start this pool's InboxPoller. Done after the
    # materialize-deps injection (the poller's lazy-materialize path reads
    # pool._materialize_deps) and before _register_main_agent.
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
        project_dir, pool_name, templates, template_registry,
        session_registry=session_registry,
        workspace_path_resolver=path_resolver,
        trace_enabled=_resolve_trace_enabled(app_config),
    )
    tool_manager.register(
        SendToAgentTool(
            store=main_store,
            source=AgentAddress(name=main_agent_name),
            broker=broker,
            registry=pool,
            agent_bus=agent_bus,
            service=main_service,
        )
    )
    main_service._target_store = main_store
    logger.info("Pool '%s': communication tool registered", pool_name)

    _wire_main_pipeline(
        pool, main_agent_name, inbox_consumer,
        notification_service,
        shared_interceptor_chain,
        im_ui, main_spec, assembly_deps, project_dir,
        command_processor, pool_name,
        tool_manager=tool_manager,
        root_provider=root_provider,
        bot_model_config=bot_model_config,
        model_choice_registry=model_choice_registry,
        cassette_recorder=cassette_recorder,
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
        media=assembly_deps.media,
        subagent_count=len(pool_spec.subagents),
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
        agent_bus=agent_bus,
        target_store=main_store,
    )


def _fallback_context_manager(main_spec: MainAgentSpec, system_prompt: str) -> Any:
    """A minimal context_manager for tests / non-workspace wiring.

    The main agent's real context manager comes from the workspace pool_data;
    this fallback keeps create_pool callable without a workspace (used by
    unit tests that mock the build steps).
    """

    return MemorySystemContextManager(
        memory_system=None,
        default_agent_id=main_spec.agent_name,
        default_agent_role="main",
        base_system_prompt=system_prompt,
        injection_policy=FullInjectionPolicy(pruned_manager=None),
        experience_manager=None,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Build steps (one method = one concern)
# ═══════════════════════════════════════════════════════════════════════════


def _placeholder_model_config() -> BotModelConfig:
    """A minimal valid BotModelConfig used when no model.yml is configured.

    Lets the bot boot so the user can configure a real model via the WebUI
    (Settings → Models) or ``modexbot config``. The placeholder provider has
    empty api_key/base_url, so every real LLM call fails — but
    ``BotModelProvider.chat_stream`` catches the provider-build failure and
    returns an ``LLMResponse(finish_reason=ERROR)``, and the ReAct LLM/end
    nodes surface that as a turn error instead of crashing the process.
    """
    return BotModelConfig(
        default_provider="_unconfigured",
        default_model="_placeholder",
        providers=[
            ProviderCfg(
                key="_unconfigured",
                name="_unconfigured",
                api_key="",
                base_url="",
                models=[
                    ModelCfg(name="_placeholder", model="_placeholder"),
                ],
            )
        ],
    )


def _resolved_or_placeholder(cfg: BotModelConfig | None) -> BotModelConfig:
    """Return ``cfg`` when a real model is configured, else the placeholder."""
    return cfg or _placeholder_model_config()


def _build_llm_provider(
    pool_name: str, bot_model_config: BotModelConfig | None
) -> BotModelProvider:
    provider = BotModelProvider(_resolved_or_placeholder(bot_model_config))
    logger.info("Pool '%s': BotModelProvider (default=%s)", pool_name, provider.model)
    return provider


# ── Terminal ─────────────────────────────────────────────────────────────


def _build_terminal_manager(
    main_spec: MainAgentSpec,
    pool_name: str,
    workspace_handle: WorkspaceHandle | None,
) -> Any | None:
    """Create terminal manager from main agent spec.

    ADR-0010 two-axis construction. The user-facing YAML fields ``use_terminal``
    (bool) and ``terminal_visibility`` (bool) live on the pool's main-agent
    ``MainAgentSpec``. The framework translates ``True`` →
    ``TerminalVisibility.VISIBLE`` and ``False`` → ``TerminalVisibility.HIDDEN``
    and constructs the manager via the two-axis
    ``create_terminal_manager(shell_info=..., visibility=...)`` signature.

    Fallback chain: if the requested VISIBLE backend cannot be created on this
    platform (``UnsupportedVisibilityForTransport``), retry with HIDDEN. If HIDDEN
    also fails, fall back to SubprocessTool-only (return None) so the agent still
    works. The shell info is auto-detected via ``detect_platform_shell``.

    ADR-0010 Consequences: the degradation decision (VISIBLE → HIDDEN) belongs
    HERE at pool-build time, not on the first command. ``create_terminal_manager``
    itself eagerly probes the (transport, visibility) combo, so an unsupported
    combination surfaces as ``UnsupportedVisibilityForTransport`` from the call
    below (validation is encapsulated in the factory, not duplicated here).
    """
    if not main_spec.use_terminal:
        logger.info("Pool '%s': use_terminal=false, skipping terminal tools", pool_name)
        return None

    visibility_bool: bool = main_spec.terminal_visibility

    shell_info = detect_platform_shell()
    if shell_info is None:
        logger.warning(
            "Pool '%s': no supported shell detected; falling back to SubprocessTool.",
            pool_name,
        )
        return None

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
            mgr = create_terminal_manager(
                shell_info=shell_info,
                visibility=vis,
                default_cwd=default_cwd,
            )
            logger.info(
                "Pool '%s': terminal manager created (family=%s, visibility=%s)",
                pool_name,
                shell_info.family.value,
                vis.value,
            )
            return mgr
        except UnsupportedVisibilityForTransport as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': terminal backend (family=%s, visibility=%s) unavailable: %s",
                pool_name,
                shell_info.family.value,
                vis.value,
                exc,
            )
        except Exception as exc:
            last_err = exc
            logger.warning(
                "Pool '%s': terminal backend (family=%s, visibility=%s) failed: %s",
                pool_name,
                shell_info.family.value,
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
    main_spec: MainAgentSpec,
    assembly_deps: PoolAssemblyDeps,
    terminal_manager,
    project_dir: Path,
    output_adapter,
    pool_name: str,
    data_dir: Path,
    pool_data: PoolDataSnapshot | None,
    root_provider: WorkspaceRootProvider | None,
    *,
    transcript_store: TranscriptStore | None = None,
    sessions_dir_provider: Callable[[], Path | None] | None = None,
    mcp_registry: McpConnectionRegistry | None = None,
    persistence: Any | None = None,
    app_config: Any | None = None,
) -> tuple[InMemoryToolManager, Any | None, JsonFileTodoStore]:
    """Build the main agent's tool manager from config.

    Tool assembly order: preset tools (file/search/bash gated by
    ``main_spec.tool_preset``), additive supplements (``main_spec.tool_supplements``,
    e.g. ast_grep), terminal tools (when ``terminal_manager`` is set), the
    custom send_file_to_user tool, the experience tool (when enabled), todo
    tools, and MCP tools resolved from ``main_spec.mcp`` via the registry.
    ``send_to_agent`` is registered separately in ``create_pool`` after the
    communication service is wired.

    When ``root_provider`` is given, the standard file/search/shell tools are
    wrapped via :func:`wrap_standard_tools` so their relative paths resolve
    against THIS workspace's root. Terminal tools (Command/Process/Terminal)
    stay UNWRAPPED — their cwd is the terminal manager's, bound separately.
    """
    from modex_agent.tools.presets import ToolPreset, get_preset_tools, get_supplement_tools

    tm = InMemoryToolManager(config=ToolManagerConfig())

    if pool_data is not None and pool_data.runtime_dir is not None:
        todo_dir: Path = pool_data.runtime_dir / "todos"
    else:
        todo_dir = data_dir / "runtime_state" / pool_name / "todos"
    from modex_agent.core.scope import RecordScope

    todo_scope = RecordScope(pool=pool_name)
    todo_store = build_todo_store(app_config, persistence, todo_dir, todo_scope)

    # Preset tools: file/search/bash gated by main_spec.tool_preset. A bash
    # factory is provided so FULL/READ_WRITE/READ_ONLY presets get a
    # workspace-scoped SubprocessTool; the terminal manager (when present)
    # registers the richer Command/Process/Terminal tools below.
    def _make_bash() -> Tool:
        sub = SubprocessTool(executor=SubprocessExecutor(), timeout=300)
        if root_provider is not None:
            wrapped = wrap_standard_tools([sub], root_provider)
            return wrapped[0]
        return sub

    preset = main_spec.tool_preset if main_spec.tool_preset is not None else ToolPreset.FULL
    for tool in get_preset_tools(preset, subprocess_tool_factory=_make_bash, root_provider=root_provider):
        tm.register(tool)

    # Additive supplement tools (e.g. ast_grep, todo) layered on top of the preset.
    for tool in get_supplement_tools(
        main_spec.tool_supplements, root_provider=root_provider, todo_store=todo_store
    ):
        tm.register(tool)
    if main_spec.tool_supplements:
        logger.info(
            "Pool '%s': supplement tools registered: %s",
            pool_name, [s.value for s in main_spec.tool_supplements],
        )

    # Terminal tools — registered when a terminal manager exists (replaces the
    # preset's bash tool with the stateful Command/Process/Terminal trio).
    if terminal_manager is not None:
        from modex_agent.tools.terminal import (
            CommandTool,
            ProcessRegistry,
            ProcessTool,
            TerminalTool,
        )
        from modex_agent.tools.terminal.config import TerminalRuntimeConfig

        cfg = TerminalRuntimeConfig()
        registry = ProcessRegistry(config=cfg)
        tm.register(CommandTool(manager=terminal_manager, registry=registry, config=cfg))
        tm.register(ProcessTool(registry=registry, manager=terminal_manager))
        tm.register(TerminalTool(terminal_manager))
        logger.info("Pool '%s': terminal tools registered (Command/Process/Terminal)", pool_name)

    # Custom tools
    from bot.tools.custom import SendFileToUserTool

    tm.register(
        SendFileToUserTool(
            output_adapter=output_adapter,
            transcript_store=transcript_store,
            media_config=assembly_deps.media,
            sessions_dir_provider=sessions_dir_provider,
        )
    )

    # Experience tool — always enabled for main agents (baked; not configurable).
    # The experience dir comes from the workspace's pool_data (fixed per
    # workspace); fallback to a data_dir relative path for non-workspace (test).
    from modex_agent.core.experience import PerFileExperienceMetaStore
    from modex_agent.memory.tools.experience import ExperienceTool

    if pool_data is not None:
        base_exp_dir: Path = pool_data.experience_dir
        _exp_path: Callable[[], Path] = lambda: base_exp_dir
    else:
        fallback = data_dir / "experiences" / pool_name / main_spec.agent_name

        def _exp_path() -> Path:
            return fallback

    _exp_path().mkdir(parents=True, exist_ok=True)
    exp_meta = PerFileExperienceMetaStore(_exp_path)
    tm.register(ExperienceTool(_exp_path, exp_meta))
    logger.info("Pool '%s': experience tool registered", pool_name)

    # MCP tools resolved from main_spec.mcp (registry names) — never let MCP
    # failures break the rest of the tool manager / pool creation.
    mcp_tools: list[Any] = []
    mcp_manager: Any | None = None
    if main_spec.mcp:
        try:
            mcp_tools, mcp_manager = await _load_agent_mcp_tools(
                main_spec.agent_name, list(main_spec.mcp), project_dir,
                mcp_registry=mcp_registry,
            )
        except Exception as exc:
            logger.warning(
                "Pool '%s': MCP tool loading failed, skipping: %s", pool_name, exc
            )

    for tool in mcp_tools:
        tm.register(tool)
    if mcp_tools:
        logger.info("Pool '%s': %d MCP tools registered", pool_name, len(mcp_tools))

    logger.info("Pool '%s': ToolManager ready (%d tools total)", pool_name, len(tm.list_tools()))
    return tm, mcp_manager, todo_store


# ── Main agent tool-name resolver (pure, for parity testing) ─────────────


def build_main_agent_tool_names(
    tool_preset: str,
    supplements: list[str],
    use_terminal: bool,
) -> set[str]:
    """Return the set of tool NAMES the main agent will receive.

    Pure projection of the main-agent tool assembly (Task 1.6 parity
    helper). Mirrors :func:`_build_tools` + ``send_to_agent``:
    preset-gated file/search/bash + supplement tools (e.g. ast_grep) +
    terminal tools (when ``use_terminal``) + the always-on send_to_agent.
    Bot-specific tools (send_file_to_user, todo, experience) and MCP tools
    are excluded from this projection — they are runtime/path-dependent and
    not governed by the preset/supplement policy.

    A ``subprocess_tool_factory`` is supplied to ``get_preset_tools`` so the
    preset's bash tool (``SubprocessTool.name == "bash"``) is included for
    FULL/READ_WRITE/READ_ONLY — matching what ``_build_tools`` registers.
    When ``use_terminal`` is set, ``_build_tools`` ADDITIONALLY registers
    the stateful Command/Process/Terminal trio; ``CommandTool.name`` is also
    ``"bash"`` (it supersedes the preset's SubprocessTool under the same
    name), so the projected set gains ``process`` and ``terminal`` but no
    extra ``bash`` entry (sets dedupe).
    """
    names: set[str] = set()
    preset = ToolPreset(tool_preset)

    def _make_bash() -> Tool:
        return SubprocessTool(executor=SubprocessExecutor(), timeout=300)

    # File/search/bash tool names per preset. The factory mirrors _build_tools'
    # _make_bash so the bash name surfaces for FULL/READ_WRITE/READ_ONLY.
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


# ── Runtime stores ───────────────────────────────────────────────────────


# NOTE: _build_runtime_stores was removed — runtime stores (turn/command/trace)
# are now built inside Workspace.build_pool_data and resolved per turn from the
# PoolData snapshot. The agent factory no longer takes a turn_store / trace_store;
# the pipeline resolves them from the workspace snapshot.


# ── Skill manager ────────────────────────────────────────────────────────


def _build_skill_manager(main_agent_name: str, project_dir: Path, pool_name: str):
    """Convention: skills/{pool_name}/{agent_name}/."""
    directories = [project_dir / "skills" / pool_name / main_agent_name]

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


def _resolve_trace_enabled(app_config: AppConfig | None) -> bool:
    if app_config is None or app_config.observability is None:
        return True
    return app_config.observability.trace_backend != TraceBackend.OFF


def _resolve_cassette_config(
    app_config: AppConfig | None, data_dir: Path
) -> tuple[bool, CassetteScope, Path]:
    base_dir = data_dir / "cassette"
    if app_config is None or app_config.observability is None:
        return False, CassetteScope.DEFAULT, base_dir
    return app_config.observability.cassette_enabled, app_config.observability.cassette_scope, base_dir


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


# ── Main agent registration ──────────────────────────────────────────────


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


# ── Communication ────────────────────────────────────────────────────────


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

    ADR-0015 D5: the service is a pure router — it no longer takes the ~30
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
                name=t.spec.agent_name,
                kind=AgentCommKind.SUBAGENT,
                description=t.spec.description,
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

    The experience review hook and turn_store are NOT wired here — the review
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
            "Pool '%s': cannot wire pipeline — main_instance=%s",
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

    # Runtime wiring
    pipeline.interceptor_chain = shared_interceptor_chain
    pipeline._user_interface = im_ui
    pipeline.governance = create_governance(assembly_deps.memory)

    # Approval runtime — main agent only (subagents never pass through this
    # function). Opt-in: build_approval_runtime returns None when disabled or
    # no tools gated, leaving runtime_services untouched (default-off).
    from modex_agent.ioc.factories.approval import build_approval_runtime
    from modex_agent.runtime.services import AgentRuntimeServices

    approval_runtime = build_approval_runtime(
        main_spec.approval, project_root=project_dir, root_provider=root_provider
    )
    # Sparse services: hooks/interceptors/governance stay None and are
    # sourced per-field from the builder defaults at turn time (identical
    # to the pre-wiring path). safety is passed explicitly because
    # AgentRuntimeServices.safety has a default_factory that would
    # otherwise clobber the pipeline's configured policy.
    # model_capabilities threads the per-pool modality declaration so
    # the inline renderer can bind to it per turn (ADR-0014 §1/§3).
    resolved_cfg = _resolved_or_placeholder(bot_model_config)
    default_resolved = resolved_cfg.default_resolved()
    services_kwargs: dict[str, Any] = dict(
        safety=pipeline.safety,
        model_capabilities=default_resolved.capabilities,
    )
    if approval_runtime is not None:
        services_kwargs["approval"] = approval_runtime
    pipeline.runtime_services = AgentRuntimeServices(**services_kwargs)

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


def _add_hook(pipeline: Any, hook: Any) -> None:
    if pipeline.hook_runner is not None:
        pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
    else:
        pipeline.hooks.append(hook)
