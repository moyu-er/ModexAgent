"""BotService core — generic bot orchestration for any InputAdapter/OutputAdapter pair.

Supports two runtime modes:
- pipeline: single AgentPipeline.
- pool: AgentPool with resident agents, BrokerBridgeService routes messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from framework.core.experience.curator import ExperienceCurator
from framework.workspace import DefaultWorkspaceContext, WorkspaceSwitchCallback

if TYPE_CHECKING:
    from framework.commands.processor import SlashCommandProcessor
    from framework.core.experience.manager import ExperienceManager
    from framework.hook.builtin.experience_review import ExperienceReviewHook
    from framework.memory.pruned.manager import PrunedManager
    from framework.runtime.codec import RuntimeStateCodecRegistry
    from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore

from bot.plugins.integration import PluginIntegration
from bot.utils.config_loader import ConfigLoader
from framework import (
    AgentPipeline,
    InMemoryToolManager,
    LLMProvider,
    ReActAgent,
    ToolManagerConfig,
)
from framework.approval.ui import IMUserInterface
from framework.control.channel import InMemoryControlChannel
from framework.control.event_bus import CallbackControlEventBus
from framework.core.context import ContextManager
from framework.core.emitter import ContentEmitter
from framework.core.llm_struct import (
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from framework.core.skills import (
    DefaultSkillBuilder,
    DirectorySkillCache,
    FileSkillSource,
    ResolutionContext,
    SkillManager,
)
from framework.hook.abc import Hook
from framework.hook.builtin import InboxFlushHook
from framework.hook.runner import HookRunner
from framework.interceptor.builtin import (
    ToolResultLimitInterceptor,
)
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
from framework.interceptor.chain import InterceptorChain
from framework.ioc.configs.agent import AgentConfig as IOCAgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.memory import MemoryConfig as IOCMemoryConfig
from framework.ioc.factories.governance import create_governance
from framework.ioc.factories.llm import create_llm_provider
from framework.ioc.factories.memory import create_memory
from framework.memory.consolidation.dream_engine import DreamEngine
from framework.memory.core.scope import MemoryContext
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.injection import FullInjectionPolicy
from framework.memory.system import MemorySystemContextManager
from framework.messaging.broker_bridge import (
    BrokerBridgeService,
)
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentAddress,
    AgentDescriptor,
    AgentFactory,
    AgentPool,
    CommunicationTracker,
    DefaultAgentFactory,
    DefaultMeshRouter,
    SessionRetentionPolicy,
)
from framework.multi_agent.bus import AgentMessageBus, LocalAgentMessageBus
from framework.multi_agent.descriptor import AgentLLMConfig
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.pipeline.adapters import InputAdapter, OutputAdapter
from framework.runtime.services import AgentRuntime
from framework.runtime.store import TurnStateStore
from framework.tools.mcp.manager import MCPClientManager
from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.terminal.manager import TerminalManager

from .builders import AgentBuilderMixin
from .pool_builder import create_pool
from .pool_instance import PoolInstance
from .pool_router import PoolRouter, PoolSessionStore

logger = logging.getLogger(__name__)

# ── Workspace data subdirectory constants ──────────────────────────────
# Used by _ws_* helpers so the layout is defined once and shared between
# initial pool creation and cd/exit rebuilds.

_SUBDIR_MEMORY = "memory"
_SUBDIR_RUNTIME = "runtime_state"
_SUBDIR_APPROVAL = "approval"
_SUBDIR_INBOX = "inbox"


def _update_pruned_manager(
    context_manager: MemorySystemContextManager,
    pruned_manager: PrunedManager | None,
) -> None:
    """Update the cached pruned_manager inside the injection policy.

    After a workspace switch the MemorySystem (and its PrunedManager) are
    replaced, but ``FullInjectionPolicy`` stores its own reference that is
    NOT derived from ``memory_system.pruned_manager`` at injection time.
    This helper keeps the two in sync so pruned catalog injection uses the
    current data directory.
    """
    policy = context_manager.injection_policy
    if hasattr(policy, "_pruned_manager"):
        policy._pruned_manager = pruned_manager


class _WorkspaceCallbackAdapter(WorkspaceSwitchCallback):
    """Adapter that wraps an async method as a WorkspaceSwitchCallback.

    Avoids defining one-shot inner classes in BotService.initialize().
    """

    __slots__ = ("_fn",)

    def __init__(self, fn: Callable[[Path, Path], Coroutine[Any, Any, None]]) -> None:
        self._fn = fn

    async def on_workspace_switch(self, old_data_dir: Path, new_data_dir: Path) -> None:
        await self._fn(old_data_dir, new_data_dir)


class BotService(AgentBuilderMixin):
    """Generic bot service supporting arbitrary InputAdapter/OutputAdapter pairs.

    Can be used for QQ, Discord, Feishu, DingTalk, Telegram, CLI, etc.
    Just provide the corresponding adapters and an Emitter factory.

    Modes:
    - pipeline: single AgentPipeline (default).
    - pool: resident AgentPool with MessageBroker routing.

    Accepts an IOC AppConfig object as the single source of truth.
    """

    def __init__(
        self,
        config_dir: Path,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Callable[[str], ContentEmitter],
        mode: Literal["pipeline", "pool"] = "pipeline",
        *,
        app_config: AppConfig | None = None,
    ):
        self.config_dir = config_dir
        self.config_loader = ConfigLoader(config_dir)
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory
        self.mode = mode
        self._app_config = app_config

        # Components (single-pool fields for pipeline mode)
        self.pipeline: AgentPipeline | None = None
        self.agent_pool: AgentPool | None = None
        self.broker_bridge: BrokerBridgeService | None = None
        self.agent_bus: AgentMessageBus | None = None
        self.tool_manager: InMemoryToolManager | None = None
        self.mcp_manager: MCPClientManager | None = None
        self.memory_system: DefaultMemorySystem | None = None
        self.context_manager: ContextManager | None = None
        self.agent: ReActAgent | None = None
        self.agent_factory: AgentFactory | None = None
        self.communication_tracker: CommunicationTracker | None = None
        self.broker: InMemoryMessageBroker | None = None
        self.inbox_server: LocalFileInboxServer | None = None
        self.inbox_producer: InboxProducer | None = None
        self.inbox_consumer: InboxConsumer | None = None

        # Terminal management
        self.terminal_manager: TerminalManager | None = None

        # Runtime components
        self.provider: LLMProvider | None = None

        # Multi-pool (for pool mode)
        self._pools: dict[str, PoolInstance] = {}
        self.pool_router: PoolRouter | None = None
        self.plugin_integration: PluginIntegration | None = None
        self.dream_engine: DreamEngine | None = None

        # Subagent caches
        self._subagent_skill_managers: dict[str, SkillManager] = {}
        self._subagent_memory_systems: dict[str, DefaultMemorySystem] = {}
        self._additional_subagent_memory_systems: dict[str, DefaultMemorySystem] = {}

        # Maintenance
        self._maintenance_task: asyncio.Task | None = None

        # Overflow cleaner
        self._overflow_cleaner: OverflowCleaner | None = None

        # Runtime codec (stored for workspace switch rebuild)
        self._runtime_codec_registry: RuntimeStateCodecRegistry | None = None

        # Control plane
        self.control_channel: InMemoryControlChannel | None = None
        self.command_processor: SlashCommandProcessor | None = None
        self.interceptor_chain: InterceptorChain | None = None
        self._safety_policy_cache: RuntimeSafetyPolicy | None = None

        # Observability
        self._event_bus: CallbackControlEventBus | None = None
        self._trace_writer: object | None = None

        # Approval
        self._approval_workspace: Path | None = None
        self._im_ui: IMUserInterface | None = None
        self._turn_store: TurnStateStore | None = None
        self._command_store: object | None = None

        # Workspace context (injected via initialize)
        self.workspace_context: DefaultWorkspaceContext | None = None

        # Router task
        self._router_task: asyncio.Task | None = None
        self._dream_task: asyncio.Task | None = None
        self._dream_interval: int = 600

        # Experience layer (initialized lazily in initialize())
        self._experience_manager: ExperienceManager | None = None
        self._experience_hook: ExperienceReviewHook | None = None
        self._experience_curator: ExperienceCurator | None = None

        # Runtime control
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def _default_pool_name(self) -> str:
        """Default pool name from config."""
        if self._app_config is not None:
            return self._app_config.multi_agent.default_pool
        return "main"

    @property
    def _main_agent_cfg(self) -> IOCAgentConfig | None:
        """Find main agent by role, not by index."""
        if not self._app_config or not self._app_config.agents:
            return None
        for a in self._app_config.agents:
            if a.role == "main":
                return a
        return self._app_config.agents[0]

    @property
    def _main_memory_cfg(self) -> IOCMemoryConfig | None:
        """Memory config for the main agent."""
        if self._main_agent_cfg is None:
            return None
        return self._main_agent_cfg.memory

    def _load_app_config(self) -> AppConfig:
        """Load IOC AppConfig from bot_config.yml."""
        import os

        path = os.path.join(self.config_dir, "bot_config.yml")
        return AppConfig.from_yaml(path)

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #

    # -- workspace data subdirectories (single source of truth) --
    # Both initialize() and workspace-switch callbacks use these so the
    # on-disk layout never diverges between initial creation and cd/exit.

    @staticmethod
    def _ws_memory(data_dir: Path) -> Path:
        return data_dir / _SUBDIR_MEMORY

    @staticmethod
    def _ws_runtime(data_dir: Path) -> Path:
        return data_dir / _SUBDIR_RUNTIME

    @staticmethod
    def _ws_approval(data_dir: Path) -> Path:
        return data_dir / _SUBDIR_APPROVAL

    @staticmethod
    def _ws_inbox(data_dir: Path) -> Path:
        return data_dir / _SUBDIR_INBOX

    @staticmethod
    def _ws_experience(data_dir: Path, pool_name: str = "main", agent_name: str = "main") -> Path:
        """Experience directory = data_dir / experiences / pool_name / agent_name.

        Resides under .modex/ alongside memory/, runtime_state/, etc.
        """
        return data_dir / "experiences" / pool_name / agent_name

    @property
    def _project_dir(self) -> Path:
        """Project root directory (where bot_service.py lives).

        resolve() ensures the path is absolute even when __file__ is relative,
        which can happen when running via python examples/bot_project/bot_service.py
        from a different CWD (common in production deployments).
        """
        return Path(__file__).resolve().parent.parent.parent

    def _resolve_path(self, config_key: str, default_relative: str) -> Path:
        """Resolve a path from AppConfig paths, falling back to a relative default."""
        assert self._app_config is not None, "AppConfig not loaded"
        paths = self._app_config.paths
        config_value = getattr(paths, config_key, None)
        if config_value:
            return (self._project_dir / config_value).resolve()
        return (self._project_dir / default_relative).resolve()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        """Initialize all components."""
        print("=" * 60)
        print(">> Initializing Bot Service")
        print(f"   mode={self.mode}")
        print("=" * 60)

        # 1. Load config (IOC AppConfig is the only source of truth)
        if self._app_config is None:
            self._app_config = self._load_app_config()
        assert self._app_config is not None, "AppConfig must be loaded before initialize"
        print(f"[OK] Config loaded ({len(self._app_config.agents)} agents via IOC)")

        # 1.5 Create WorkspaceContext
        from framework.workspace.context import DefaultWorkspaceContext

        self.workspace_context = DefaultWorkspaceContext(
            home=self._project_dir,
            active_checker=self._make_active_checker(),
        )
        assert self.workspace_context is not None

        # Restore last workspace
        restore_result = await self.workspace_context.restore()
        if restore_result and restore_result.success:
            print(f"[OK] Restored workspace: {restore_result.current_path}")
        else:
            print(f"[OK] Using default workspace: {self._project_dir}")

        # Register workspace switch callbacks
        self.workspace_context.register_callback(
            _WorkspaceCallbackAdapter(self._on_ws_stop_and_rebuild)
        )
        self.workspace_context.register_callback(
            _WorkspaceCallbackAdapter(self._on_ws_terminal_reset)
        )

        # 2. Create Broker
        self.broker = InMemoryMessageBroker()
        await self.broker.start()
        print("[OK] Broker initialized")

        # 2.5 Build shared interceptor chain (used by both modes)
        self.interceptor_chain = self._build_interceptor_chain()

        # 2.6 Build shared control channel
        self.control_channel = self._build_control_channel()

        # ── Pool mode: dispatch early, skip pipeline-specific setup ──
        if self.mode == "pool":
            # Plugins are OFF by default in pool mode.
            from bot.plugins.integration import PluginIntegration as _PI

            self.plugin_integration = _PI(config={"enabled": False})
            await self._initialize_pool()
            self._print_pool_info()
            return

        # ── Pipeline mode continues below ──

        # 3. Create ToolManager
        self.tool_manager = InMemoryToolManager(config=ToolManagerConfig())

        # 3a. Create TerminalManager — degrade to subprocess only when no shell at all.
        main_cfg = self._main_agent_cfg
        if main_cfg and main_cfg.use_terminal:
            from framework.tools.terminal.types import detect_platform_shell

            shell_info = detect_platform_shell()
            if shell_info is not None:
                try:
                    from framework.tools.terminal import TerminalManager

                    visibility: bool = getattr(main_cfg, "terminal_visibility", True)
                    self.terminal_manager = TerminalManager(
                        max_terminals=getattr(self._app_config, "terminal", {}).get(
                            "max_terminals", 5
                        ),
                        shell_info=shell_info,
                        visibility=visibility,
                    )
                    print(
                        f"[OK] TerminalManager initialized ({shell_info.family.value}: {shell_info.path}, {visibility}, lazy)"
                    )
                except Exception as e:
                    logger.warning("TerminalManager initialization failed: %s", e)
                    self.terminal_manager = None
            else:
                self.terminal_manager = None
                print("[INFO] No shell found — terminal tools disabled, subprocess only")
        else:
            self.terminal_manager = None
            print("[INFO] TerminalManager disabled (use_terminal=false)")

        await self._register_tools(terminal_manager=self.terminal_manager)
        await self._register_mcp_tools()
        print(
            f"[OK] ToolManager initialized, {len(self.tool_manager.list_tools())} tools registered"
        )

        # -- Plugin system --
        # Plugins are OFF by default. Only enabled if `plugins:` section
        # in bot_config.yml has `enabled: true`.
        plugins_cfg = self._app_config.plugins
        if plugins_cfg is not None and plugins_cfg.enabled:
            local_plugins_dir = self._project_dir / "plugins"
            self.plugin_integration = PluginIntegration(
                plugins_cfg.model_dump(),
                extra_plugin_dirs=[local_plugins_dir] if local_plugins_dir.exists() else [],
            )
            has_plugins = await self.plugin_integration.discover_and_load()
            if has_plugins:
                self.plugin_integration.inject_tools(self.tool_manager)
                print(
                    f"[OK] Plugin system loaded, {len(self.plugin_integration.list_plugins())} plugins active"
                )
            else:
                print("[INFO] No plugins found")
        else:
            # Stub integration: no-op for downstream injection calls.
            from bot.plugins.integration import PluginIntegration as _PI

            self.plugin_integration = _PI(config={"enabled": False})
            print("[INFO] Plugins disabled (no `plugins.enabled: true` in config)")

        # 4. Create LLM Provider
        self.provider = create_llm_provider(self._app_config.llm, self._app_config.safety)
        print(f"[OK] LLM Provider: {self._app_config.llm.model}")

        # 5. Initialize MemorySystem using IOC factory
        data_dir = self.workspace_context.data_dir
        memory_dir = self._ws_memory(data_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)
        main_cfg = self._main_agent_cfg
        main_memory_cfg = main_cfg.memory if main_cfg else self._app_config.memory
        self.memory_system = create_memory(
            main_memory_cfg or IOCMemoryConfig(),
            self.provider,
            memory_dir,
        )
        await self.memory_system.initialize()

        self.pruned_manager = self.memory_system.pruned_manager
        self.context_manager = MemorySystemContextManager(
            memory_system=self.memory_system,
            default_agent_id=main_cfg.name if main_cfg else "main",
            default_agent_role="main",
            base_system_prompt=main_cfg.system_prompt if main_cfg else "",
            injection_policy=FullInjectionPolicy(pruned_manager=self.pruned_manager),
        )
        print(f"[OK] MemorySystem initialized (registry: {memory_dir})")

        # Inject plugin Memory Providers
        await self.plugin_integration.inject_memory_providers(
            self.memory_system,
            init_kwargs={
                "llm_provider": self.provider,
                "workspace": data_dir,
            },
        )

        # Inject plugin MemorySystem modifiers (e.g. tool_call_cleanup)
        self.plugin_integration.inject_memory_system_modifiers(self.memory_system)

        # 5.5 Initialize long-term defaults, maintenance, and dream engine
        await self._init_long_term_defaults(data_dir, main_memory_cfg)
        await self._init_maintenance_task(main_memory_cfg)
        self._init_dream()

        # Wire archive trigger callback so cleanup_session can check archive
        # and trigger DreamEngine whenever unprocessed archives exist.
        if self.memory_system is not None:
            self.memory_system.set_archive_trigger_callback(self._archive_trigger)

        # 6. Create SkillManager (main agent has its own)
        main_skill_manager: SkillManager | None = None
        main_skills_dir = self._resolve_path("skills_dir", "skills/main")
        if main_skills_dir.exists():
            source = FileSkillSource(
                directories=[main_skills_dir],
                cache=True,
                layout="directory",
                skill_filename="SKILL.md",
            )
            cache = DirectorySkillCache(
                directories=[main_skills_dir],
                layout="directory",
            )
            builder = DefaultSkillBuilder(base_path=self._project_dir)
            main_skill_manager = SkillManager(
                source=source,
                builder=builder,
                cache=cache,
            )
            available_skills = await main_skill_manager.list_skills(
                ResolutionContext.from_runtime(tool_manager=self.tool_manager)
            )
            print(
                f"[OK] SkillManager initialized, main agent loaded {len(available_skills)} skills"
            )

            self.plugin_integration.inject_skill_sources(main_skill_manager)
            print("[OK] Plugin skill sources injected")
        else:
            print(f"[WARN] Skills directory not found: {main_skills_dir}")

        # ---- Experience Manager (only for agents with experience.enabled) ----
        main_cfg = self._main_agent_cfg
        if main_cfg is not None and main_cfg.experience is not None:
            exp_cfg = main_cfg.experience
            if exp_cfg.enabled:
                from framework.agents.experience.review_agent import ExperienceReviewAgent
                from framework.core.experience.manager import ExperienceManager
                from framework.core.experience.meta import PerFileExperienceMetaStore
                from framework.core.experience.source import FileExperienceSource
                from framework.hook.builtin.experience_review import ExperienceReviewHook

                # Shared dynamic path lambda — workspace-safe via workspace_context
                def _exp_path() -> Path:
                    return BotService._ws_experience(
                        self.workspace_context.data_dir,
                        pool_name=self._default_pool_name,
                        agent_name=main_cfg.name,
                    )

                _exp_path().mkdir(parents=True, exist_ok=True)

                exp_source = FileExperienceSource(directories=[_exp_path()])
                self._experience_manager = ExperienceManager(source=exp_source)

                if self.context_manager is not None:
                    self.context_manager._experience_manager = self._experience_manager

                exp_meta = PerFileExperienceMetaStore(_exp_path)
                exp_review_agent = ExperienceReviewAgent(
                    provider=self.provider,
                    max_iterations=exp_cfg.max_iterations,
                )
                exp_review_hook = ExperienceReviewHook(
                    review_agent=exp_review_agent,
                    experience_dir=_exp_path,
                    meta_store=exp_meta,
                    min_messages=exp_cfg.min_messages,
                    exp_cooldown_turns=exp_cfg.exp_cooldown_turns,
                )

                self._experience_hook = exp_review_hook
                self._experience_review_agent = exp_review_agent

                # ---- Experience Tool (dynamic path via lambda — workspace-safe) ----
                from framework.memory.tools.experience import ExperienceTool

                self.tool_manager.register(ExperienceTool(_exp_path, exp_meta))
                print("   [OK] experience tool registered (action=read/write/edit/list/rename)")

                # ---- Experience Curator (background lifecycle) ----
                from framework.core.experience.curator import ExperienceCurator

                exp_curator = ExperienceCurator(
                    experience_dir=_exp_path,
                    meta_store=exp_meta,
                    max_experiences=exp_cfg.max_experiences,
                )
                self._experience_curator = exp_curator
                self._experience_curator_task = asyncio.create_task(
                    self._curator_background_loop(exp_curator, exp_cfg.curator_interval),
                )
                self._tasks.append(self._experience_curator_task)

                print(f"[OK] Experience layer initialized (dir: {_exp_path()})")

        # 6.5 Create InboxServer / Producer / Consumer
        inbox_dir = self._ws_inbox(data_dir)
        self.inbox_server = LocalFileInboxServer(workspace=inbox_dir)
        self.inbox_producer = InboxProducer(server=self.inbox_server)
        self.inbox_consumer = InboxConsumer(server=self.inbox_server)
        print(f"[OK] InboxServer initialized (storage: {inbox_dir})")

        # Collect runtime hooks for factory injection
        runtime_hooks = self._collect_run_hooks()

        # 7. Create AgentFactory (with runtime components)
        hook_runner = self._build_hook_runner(runtime_hooks)
        interceptor_chain = self._build_interceptor_chain()

        # Start overflow cleaner
        if self.interceptor_chain is not None:
            for interceptor in self.interceptor_chain.interceptors:
                if isinstance(interceptor, ToolResultLimitInterceptor):
                    if interceptor.handler is not None:
                        self._overflow_cleaner = interceptor.handler._cleaner
                        break

        self.agent_factory = DefaultAgentFactory(
            default_llm_provider=self.provider,
            default_tool_manager=self.tool_manager,
            skill_manager=main_skill_manager,
            inbox_server=self.inbox_server,
            default_hooks=runtime_hooks,
            default_hook_runner=hook_runner,
            default_interceptor_chain=interceptor_chain,
        )

        # 7.5 Initialize approval infrastructure
        self._approval_workspace = self._ws_approval(data_dir)
        self._im_ui = IMUserInterface(
            output_adapter=self.output_adapter,
            channel=self.control_channel,
        )
        print(f"[OK] Approval infrastructure initialized (workspace: {self._approval_workspace})")

        # 7.6 Initialize typed runtime stores (TurnStateStore + RuntimeCommandStore)
        from framework.agents.react.state import ReActRuntimeStateCodec
        from framework.runtime.codec import RuntimeStateCodecRegistry
        from framework.runtime.enums import AgentKind
        from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore

        runtime_data_dir = self._ws_runtime(data_dir)
        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        self._runtime_codec_registry = codec_registry
        self._turn_store = JsonFileTurnStateStore(runtime_data_dir / "turns", codec_registry)
        self._command_store = JsonFileRuntimeCommandStore(runtime_data_dir / "commands")
        print("[OK] Typed runtime stores initialized (data/runtime_state/)")

        # 8. Create ReActAgent (main agent in full mode with approval)
        self.agent = ReActAgent(provider=self.provider, mode="full")
        print("[OK] ReActAgent initialized")

        # 9. Initialize pipeline runtime
        await self._initialize_pipeline(main_skill_manager)
        # 10. Register multi-agent tools
        await self._register_multi_agent_tools()
        print(f"[OK] Multi-agent tools registered, total: {len(self.tool_manager.list_tools())}")

        # 11. Display LLM config
        print("\n[INFO] LLM config:")
        print(f"   Model: {self._app_config.llm.model}")
        print(f"   max_tokens: {self._app_config.llm.max_tokens}")
        print(f"   temperature: {self._app_config.llm.temperature}")

        # 12. Display architecture info
        print("\n[ARCH] Components:")
        print(f"   - InputAdapter: {self.input_adapter.name}")
        print(f"   - OutputAdapter: {self.output_adapter.name}")
        print(f"   - ToolManager: {type(self.tool_manager).__name__}")
        print(f"   - ContextManager: {type(self.context_manager).__name__}")
        print(f"   - Agent: {self.agent.name}")
        print(f"   - AgentFactory: {type(self.agent_factory).__name__}")
        print("   - InboxServer: LocalFileInboxServer")
        print(f"   - Mode: {self.mode}")

        print("=" * 60)

    async def _initialize_pipeline(self, main_skill_manager: SkillManager | None) -> None:
        """Initialize pipeline-mode runtime."""
        assert self._app_config is not None, "AppConfig not loaded"
        assert self.workspace_context is not None, "WorkspaceContext not initialized"
        if self.broker is None:
            raise RuntimeError("Broker is not initialized")
        if self.agent_factory is None:
            raise RuntimeError("AgentFactory is not initialized")
        if self.inbox_producer is None:
            raise RuntimeError("InboxProducer is not initialized")
        if self.inbox_consumer is None:
            raise RuntimeError("InboxConsumer is not initialized")

        main_cfg = self._main_agent_cfg
        parent_agent_name = main_cfg.name if main_cfg else "main"
        main_address = AgentAddress(kind="agent", name=parent_agent_name)
        main_descriptor = AgentDescriptor(
            address=main_address,
            llm_config=AgentLLMConfig(
                model=self._app_config.llm.model,
                temperature=self._app_config.llm.temperature,
                max_tokens=self._app_config.llm.max_tokens,
            ),
            system_prompt_template=main_cfg.system_prompt if main_cfg else "",
            context_strategy="persistent",
            max_iterations=main_cfg.max_steps if main_cfg else 40,
            execution_strategy="react",
            safety_policy=self.safety_policy,
        )
        inbox_flush_hook = InboxFlushHook(
            consumer=self.inbox_consumer,
            agent_name=parent_agent_name,
        )

        if self.agent is None:
            raise RuntimeError("Agent is not initialized")
        if self.tool_manager is None:
            raise RuntimeError("ToolManager is not initialized")

        pipeline_hooks: list[Hook[Any]] = [inbox_flush_hook]  # type: ignore[type-arg]

        if self._experience_hook is not None:
            pipeline_hooks.append(self._experience_hook)

        pipeline_hooks.extend(self._collect_run_hooks())

        # Pipeline mode observability
        from framework.control import CallbackControlEventBus, ControlEventType
        from framework.hook.builtin import ProgressReportHook, TraceFileWriter

        self._event_bus = CallbackControlEventBus()
        trace_dir = self._project_dir / "logs"
        trace_dir.mkdir(exist_ok=True)
        self._trace_writer = TraceFileWriter(path=trace_dir / "trace.jsonl")
        await self._event_bus.subscribe(ControlEventType.AGENT_PROGRESS, self._trace_writer.handle)
        pipeline_hooks.append(ProgressReportHook(event_bus=self._event_bus))

        # Build AgentRuntime via framework RuntimeAssembler
        runtime = await self._assemble_runtime(hooks=self._build_hook_runner(pipeline_hooks))
        command_processor = self._build_main_command_processor(
            main_skill_manager,
            workspace_ctx=self.workspace_context,
        )
        self.command_processor = command_processor

        self.pipeline = AgentPipeline(
            agent=self.agent,
            context_manager=self.context_manager,
            tool_manager=self.tool_manager,
            input_adapter=self.input_adapter,
            output_adapter=self.output_adapter,
            emitter_factory=self.emitter_factory,
            dream_engine=self.dream_engine,
            dream_interval=self._dream_interval,
            max_iterations=main_cfg.max_steps if main_cfg else 40,
            skill_manager=main_skill_manager,  # type: ignore[arg-type]
            hook_runner=self._build_hook_runner(pipeline_hooks),
            interceptor_chain=self.interceptor_chain,
            context_manager_factory=self._get_context_manager,
            governance=create_governance(self._main_memory_cfg, self._app_config.llm.max_tokens),
            safety=self.safety_policy,
            approval_workspace=str(self._approval_workspace),
            user_interface=self._im_ui,
            turn_store=self._turn_store,
            command_store=self._command_store,
            runtime_services=runtime.services,
            control_channel=self.control_channel,
            command_processor=command_processor,
            router=DefaultMeshRouter(),
            agent_descriptor=main_descriptor,
        )
        # Configure control command interception on the input adapter
        if self.control_channel is not None and self.command_processor is not None:
            self.input_adapter.configure_control_filter(
                control_channel=self.control_channel,
                command_processor=self.command_processor,
                output_adapter=self.output_adapter,
                session_checker=self.pipeline.is_session_active if self.pipeline else None,
                turn_uuid_getter=self.pipeline.get_active_turn_uuid if self.pipeline else None,
            )

        print("[OK] AgentPipeline initialized")
        print(f"   Input: {self.input_adapter.name}")
        print(f"   Output: {self.output_adapter.name}")

    async def _initialize_pool(self) -> None:
        """Initialize pool-mode with multiple pools."""
        assert self._app_config is not None, "AppConfig not loaded"
        assert self.workspace_context is not None, "WorkspaceContext not initialized"
        pool_configs = self._app_config.pools
        if not pool_configs:
            raise RuntimeError("No pools defined. Add .yml files to config/pools/")

        # 1. Shared infra: Inbox + AgentMessageBus
        inbox_dir = self._ws_inbox(self.workspace_context.data_dir)
        self.inbox_server = LocalFileInboxServer(workspace=inbox_dir)
        self.inbox_producer = InboxProducer(server=self.inbox_server)
        self.inbox_consumer = InboxConsumer(server=self.inbox_server)
        self.agent_bus = LocalAgentMessageBus(
            producer=self.inbox_producer,
            consumer=self.inbox_consumer,
            broker=self.broker,
        )
        print(f"[OK] Inbox + AgentMessageBus initialized ({inbox_dir})")

        # 2. Shared infra: Approval
        self._approval_workspace = self._ws_approval(self.workspace_context.data_dir)
        self._im_ui = IMUserInterface(
            output_adapter=self.output_adapter,
            channel=self.control_channel,
        )

        # 4. Shared infra: Hooks & Interceptors
        shared_hooks = self._collect_run_hooks()

        # 4b. Shared infra: Observability event bus + trace writer + progress report hook
        from framework.control import CallbackControlEventBus, ControlEventType
        from framework.hook.builtin import ProgressReportHook, TraceFileWriter

        self._event_bus = CallbackControlEventBus()
        trace_dir = self._project_dir / "logs"
        trace_dir.mkdir(exist_ok=True)
        self._trace_writer = TraceFileWriter(path=trace_dir / "trace.jsonl")
        await self._event_bus.subscribe(ControlEventType.AGENT_PROGRESS, self._trace_writer.handle)
        shared_hooks.append(ProgressReportHook(event_bus=self._event_bus))

        shared_hook_runner = self._build_hook_runner(shared_hooks)
        shared_interceptor_chain = self._build_interceptor_chain()

        # 5. Shared infra: Retention & CommunicationTracker
        retention_cfg = self._app_config.multi_agent.session_retention
        retention = SessionRetentionPolicy(
            max_sessions_per_subagent=retention_cfg.max_sessions_per_subagent,
            max_sessions_global=retention_cfg.max_sessions_global,
            ttl_seconds=retention_cfg.ttl_seconds,
            cleanup_interval_seconds=retention_cfg.cleanup_interval_seconds,
        )
        self.communication_tracker = CommunicationTracker()

        # 5.5. Build shared command_processor (with cd/exit handlers for workspace switching)
        self.command_processor = self._build_main_command_processor(
            None,
            workspace_ctx=self.workspace_context,
        )

        # 6. Create all pools
        self._pools = {}
        data_dir = self.workspace_context.data_dir
        for pool_name, pool_cfg in pool_configs.items():
            print(f"\n[POOL] Creating pool '{pool_name}'...")
            self._pools[pool_name] = await create_pool(
                pool_name=pool_name,
                pool_cfg=pool_cfg,
                project_dir=self._project_dir,
                data_dir=data_dir,
                broker=self.broker,
                inbox_server=self.inbox_server,
                inbox_producer=self.inbox_producer,
                inbox_consumer=self.inbox_consumer,
                agent_bus=self.agent_bus,
                output_adapter=self.output_adapter,
                safety=self.safety_policy,
                retention=retention,
                comm_tracker=self.communication_tracker,
                approval_workspace=self._approval_workspace,
                im_ui=self._im_ui,
                shared_hooks=shared_hooks,
                shared_hook_runner=shared_hook_runner,
                shared_interceptor_chain=shared_interceptor_chain,
                control_channel=self.control_channel,
                command_processor=self.command_processor,
                workspace_context=self.workspace_context,
            )
            print(f"[OK] Pool '{pool_name}' created")

        # 7. PoolRouter
        session_store = PoolSessionStore(data_dir=data_dir)
        self.pool_router = PoolRouter(
            input_adapter=self.input_adapter,
            output_adapter=self.output_adapter,
            broker=self.broker,
            pools=self._pools,
            session_store=session_store,
            default_pool=self._app_config.multi_agent.default_pool,
        )

        await self._init_pool_dream_engine()

        # Configure control command interception (pool mode)
        if self.control_channel is not None and self.command_processor is not None:
            self.input_adapter.configure_control_filter(
                control_channel=self.control_channel,
                command_processor=self.command_processor,
                output_adapter=self.output_adapter,
                # Pool mode: no per-turn UUID tracking (turn runs in subprocess)
                session_checker=None,
                turn_uuid_getter=None,
            )

    def _print_pool_info(self) -> None:
        """Display pool configuration summary."""
        print(f"\n[INFO] Pools: {list(self._pools.keys())}")
        for name, pi in self._pools.items():
            subagent_count = sum(1 for a in pi.config.agents if a.role == "subagent")
            print(f"   {name}: {pi.main_agent_name} + {subagent_count} subagents")
        print(f"[INFO] Switch commands: /{' /'.join(self._pools.keys())}")
        print(f"[INFO] Default pool: {self._app_config.multi_agent.default_pool}")

    def _make_active_checker(self) -> Callable[[], bool]:
        """构造活跃 agent 检查器。

        检查所有 Pipeline（主 pipeline + 所有 pool 的 agent）
        是否有正在运行的 agent turn（含 subagent）。
        subagent 运行在父 agent 的 session task 中，已被覆盖。

        Returns:
            True 表示有活跃 agent，应拒绝 cd。
        """

        def check() -> bool:
            if self.pipeline is not None and self.pipeline.has_active_sessions():
                return True
            for pool_inst in self._pools.values():
                if pool_inst.pool.has_active_sessions():
                    return True
            return False

        return check

    # ------------------------------------------------------------------ #
    # Workspace switch callbacks (registered in initialize)
    # ------------------------------------------------------------------ #

    async def _on_ws_stop_and_rebuild(self, _old_dir: Path, new_dir: Path) -> None:
        """① Stop background + rebuild stores (atomic)."""
        await self._stop_background_tasks()
        self._clear_subagent_caches()
        if self.mode == "pool":
            await self._rebuild_pool_memory(new_dir)
            self._update_communication_paths(new_dir)
        else:
            await self._rebuild_pipeline_memory(new_dir)
        await self._rebuild_shared_infrastructure(new_dir)

    async def _on_ws_terminal_reset(self, _old_dir: Path, _new_dir: Path) -> None:
        """② Close all terminal sessions. _close_all_terminals covers both
        pipeline and pool terminal managers."""
        await self._close_all_terminals(suppress_errors=True)

    # ------------------------------------------------------------------ #
    # Workspace switch helpers
    # ------------------------------------------------------------------ #

    async def _close_all_terminals(self, *, suppress_errors: bool = True) -> None:
        """Close every terminal session (self + pools).

        Used by both workspace-switch callbacks and BotService.stop().
        """
        for mgr in [self.terminal_manager] + [pi.terminal_manager for pi in self._pools.values()]:
            if mgr is None:
                continue
            for name in list(mgr.list_names()):
                try:
                    await mgr.close(name)
                except BaseException:
                    if not suppress_errors:
                        raise

    def _rebuild_overflow_store(self, new_dir: Path) -> None:
        """Update overflow store workspace after workspace switch."""
        from framework.tools.overflow.local import LocalFileToolOverflowStore

        if self.interceptor_chain is None:
            return
        for interceptor in self.interceptor_chain.interceptors:
            if not isinstance(interceptor, ToolResultLimitInterceptor):
                continue
            if interceptor.handler is None:
                continue
            new_store = LocalFileToolOverflowStore(
                workspace=new_dir,
                max_chunk_size=10_000,
            )
            interceptor.handler._store = new_store
            if interceptor.handler._cleaner is not None:
                interceptor.handler._cleaner._store = new_store

    async def _rebuild_pipeline_memory(self, new_dir: Path) -> None:
        """Rebuild pipeline-mode memory + stores + experience."""
        main_cfg = self._main_agent_cfg
        main_mem = main_cfg.memory if main_cfg else self._app_config.memory
        if self.provider is None:
            return
        (
            self.memory_system,
            self._turn_store,
            self._command_store,
        ) = await self._rebuild_memory_for_target(
            new_dir,
            self._ws_memory(new_dir),
            self._ws_runtime(new_dir),
            main_mem,
            self.provider,
            self.context_manager,
            pipeline=self.pipeline,
        )
        self.pruned_manager = self.memory_system.pruned_manager
        await self._rebuild_experience(new_dir)

    async def _rebuild_pool_memory(self, new_dir: Path) -> None:
        """Rebuild pool-mode memory + runtime stores + experience for every pool.

        Also updates factory._default_turn_store and factory._trace_store so
        NEW subagents created after cd write to the new data directory.
        """
        for pool_inst in self._pools.values():
            main_inst = pool_inst.pool._agents.get(pool_inst.main_agent_name)
            (
                new_memory,
                new_turn_store,
                new_cmd_store,
            ) = await self._rebuild_memory_for_target(
                new_dir,
                self._ws_memory(new_dir) / pool_inst.name,
                self._ws_runtime(new_dir) / pool_inst.name,
                pool_inst.config.memory,
                pool_inst.provider,
                pool_inst.context_manager,
                pipeline=main_inst.pipeline if main_inst else None,
            )
            pool_inst.memory_system = new_memory
            # Update factory defaults so subagents created after cd
            # write trace/turns to the new workspace
            factory = pool_inst.pool._agent_factory
            factory._default_turn_store = new_turn_store
            if factory._trace_store is not None:
                factory._trace_store._base_dir = (
                    self._ws_runtime(new_dir) / pool_inst.name / "trace"
                )
        await self._rebuild_experience(new_dir)

    async def _stop_background_tasks(self) -> None:
        """停止后台任务：dream task + injection queues。

        必须在重建存储之前调用，确保没有后台任务写入旧路径。
        """
        if self._dream_task is not None:
            self._dream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dream_task
            self._dream_task = None
        # Collect all pipelines (pipeline mode + pool mode agents)
        pipelines: list[AgentPipeline] = []
        if self.pipeline is not None:
            pipelines.append(self.pipeline)
        for pi in self._pools.values():
            for ai in pi.pool._agents.values():
                if ai.pipeline is not None:
                    pipelines.append(ai.pipeline)
        for p in pipelines:
            p._injection_queues.clear()

    def _clear_subagent_caches(self) -> None:
        """清空 subagent 缓存的 memory/skill 引用，避免指向旧路径。"""
        for cache in (
            self._subagent_memory_systems,
            self._subagent_skill_managers,
            self._additional_subagent_memory_systems,
        ):
            cache.clear()

    def _start_dream_task(self) -> None:
        """启动 dream 后台任务（如果 dream engine 已初始化）。"""
        if self.dream_engine is None:
            return
        self._dream_task = asyncio.create_task(
            self._dream_background_loop(interval=self._dream_interval)
        )
        logger.info("DreamEngine task restarted, interval=%ds", self._dream_interval)

    async def _rebuild_memory_for_target(
        self,
        new_data_dir: Path,
        memory_dir: Path,
        runtime_dir: Path,
        memory_cfg: IOCMemoryConfig | None,
        provider: LLMProvider,
        context_manager: MemorySystemContextManager,
        *,
        pipeline: AgentPipeline | None = None,
    ) -> tuple[DefaultMemorySystem, JsonFileTurnStateStore, JsonFileRuntimeCommandStore]:
        """重建单个 target 的 memory + runtime stores。

        Pipeline 和 Pool 模式共用此方法，差异通过参数表达。

        Args:
            new_data_dir: 新的 .modex/ 根目录（用于 approval 路径）
            memory_dir: memory 创建目录（pipeline: .modex/memory, pool: .modex/memory/{name}）
            runtime_dir: runtime stores 目录（pipeline: .modex/runtime_state, pool: .modex/runtime_state/{name}）
            memory_cfg: memory 配置
            provider: LLM provider
            context_manager: 需要更新 .memory_system 的 context manager
            pipeline: 需要更新 store 引用的 pipeline（可选）

        Returns:
            (new_memory_system, new_turn_store, new_command_store)
        """
        from framework.runtime.store import JsonFileRuntimeCommandStore, JsonFileTurnStateStore

        # Close old memory
        old_memory = context_manager.memory_system
        if old_memory is not None:
            await old_memory.close()

        # Create new memory
        memory_dir.mkdir(parents=True, exist_ok=True)
        new_memory = create_memory(
            memory_cfg or IOCMemoryConfig(),
            provider,
            memory_dir,
        )
        await new_memory.initialize()

        # Update context manager + sync pruned
        context_manager.memory_system = new_memory
        _update_pruned_manager(context_manager, new_memory.pruned_manager)

        # Re-inject plugins + long-term defaults
        await self.plugin_integration.inject_memory_providers(
            new_memory,
            init_kwargs={"llm_provider": provider, "workspace": new_data_dir},
        )
        self.plugin_integration.inject_memory_system_modifiers(new_memory)
        await self._init_long_term_defaults(
            new_data_dir,
            memory_cfg,
            memory_system=new_memory,
        )

        # New runtime stores
        new_turn_store = JsonFileTurnStateStore(
            runtime_dir / "turns",
            self._runtime_codec_registry,
        )
        new_cmd_store = JsonFileRuntimeCommandStore(
            runtime_dir / "commands",
        )

        # Update pipeline refs if provided
        if pipeline is not None:
            pipeline.turn_store = new_turn_store
            pipeline.command_store = new_cmd_store
            pipeline._approval_workspace = self._ws_approval(new_data_dir)

        return new_memory, new_turn_store, new_cmd_store

    async def _rebuild_experience(self, new_data_dir: Path) -> None:
        """Rebuild the injection-side experience manager on workspace switch.

        Tools, meta_store, and curator resolve lazily (via lambda capturing
        workspace_context.data_dir) so they need no rebuild — PerFileExperienceMetaStore
        auto-resolves to the new path after /cd.  Only the prompt-injection
        ExperienceManager needs a fresh FileExperienceSource pointing at the new
        data directory.
        """
        if self._experience_manager is None:
            return
        from framework.core.experience.manager import ExperienceManager
        from framework.core.experience.source import FileExperienceSource

        def _new_manager(pool_name: str, agent_name: str) -> ExperienceManager:
            exp_dir = BotService._ws_experience(
                new_data_dir, pool_name=pool_name, agent_name=agent_name
            )
            exp_dir.mkdir(parents=True, exist_ok=True)
            return ExperienceManager(source=FileExperienceSource(directories=[exp_dir]))

        # Pipeline mode
        main_cfg = self._main_agent_cfg
        if self.context_manager is not None:
            agent_name = main_cfg.name if main_cfg else "main"
            mgr = _new_manager(self._default_pool_name, agent_name)
            self._experience_manager = mgr
            if hasattr(self.context_manager, "_experience_manager"):
                self.context_manager._experience_manager = mgr

        # Pool mode — each pool's context_manager gets its own manager
        for pool_inst in self._pools.values():
            main_agent_name = "main"
            agents = getattr(pool_inst.config, "agents", []) or []
            for a in agents:
                if getattr(a, "role", None) == "main":
                    main_agent_name = a.name
                    break
            mgr = _new_manager(pool_inst.name, main_agent_name)
            if hasattr(pool_inst.context_manager, "_experience_manager"):
                pool_inst.context_manager._experience_manager = mgr

        print(f"[OK] Experience injection rebuilt for workspace: {new_data_dir}")

    async def _rebuild_shared_infrastructure(self, new_data_dir: Path) -> None:
        """更新共享基础设施：inbox + approval + overflow + dream。

        Pipeline 和 Pool 模式共用此方法。
        """
        # Inbox
        inbox_dir = self._ws_inbox(new_data_dir)
        inbox_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_server._workspace = inbox_dir
        # Sync delivered-id tracker workspace so dedup writes to the correct dir
        if hasattr(self.inbox_server, "_tracker") and hasattr(
            self.inbox_server._tracker, "_workspace"
        ):
            self.inbox_server._tracker._workspace = inbox_dir
        # Approval
        self._approval_workspace = self._ws_approval(new_data_dir)
        # Overflow store
        self._rebuild_overflow_store(new_data_dir)
        # Dream engine + task
        if self.mode == "pool":
            await self._init_pool_dream_engine()
        else:
            self._init_dream()
        self._start_dream_task()

    def _update_communication_paths(self, new_data_dir: Path) -> None:
        """更新 pool 模式下 AgentCommunicationService 的路径引用。

        cd 切换 workspace 后，memory、pruned、runtime 都需要指向新数据目录，
        否则 trace/output 会写到旧目录。
        """
        for pool_inst in self._pools.values():
            svc = pool_inst.communication_service
            if svc is not None:
                svc._memory_dir = self._ws_memory(new_data_dir) / pool_inst.name
                svc._pruned_manager = pool_inst.memory_system.pruned_manager
                svc._runtime_dir = new_data_dir / "runtime_state" / pool_inst.name

    def _find_subagent_cfg(self) -> IOCAgentConfig | None:
        """Find the first subagent config by role."""
        if not self._app_config or not self._app_config.agents:
            return None
        for a in self._app_config.agents:
            if a.role == "subagent":
                return a
        return None

    def _find_additional_subagent_cfgs(self) -> list[IOCAgentConfig]:
        """Find all subagent configs by role, excluding the primary subagent."""
        if not self._app_config or not self._app_config.agents:
            return []
        primary = self._find_subagent_cfg()
        primary_name = primary.name if primary else None
        return [
            a for a in self._app_config.agents if a.role == "subagent" and a.name != primary_name
        ]

    @property
    def safety_policy(self) -> RuntimeSafetyPolicy:
        """Safety policy from IOC config."""
        if self._safety_policy_cache is not None:
            return self._safety_policy_cache
        if self._app_config is not None and self._app_config.safety is not None:
            s = self._app_config.safety
            policy = RuntimeSafetyPolicy(
                llm=LLMTimeoutPolicy(
                    request_timeout_seconds=s.llm.request_timeout,
                    stream_idle_timeout_seconds=s.llm.stream_idle_timeout,
                    framework_max_retries=s.llm.max_retries,
                    retry_backoff_seconds=tuple(s.llm.retry_backoff),
                ),
                turn=TurnTimeoutPolicy(
                    agent_run_timeout_seconds=s.turn.agent_run_timeout,
                    dispatch_timeout_seconds=s.turn.dispatch_timeout,
                    hook_timeout_seconds=s.turn.hook_timeout,
                    tool_timeout_seconds=s.turn.tool_timeout,
                ),
            )
        else:
            policy = RuntimeSafetyPolicy(
                llm=LLMTimeoutPolicy(
                    request_timeout_seconds=45.0,
                    stream_idle_timeout_seconds=90.0,
                    framework_max_retries=1,
                    retry_backoff_seconds=(2.0, 8.0),
                ),
                turn=TurnTimeoutPolicy(
                    agent_run_timeout_seconds=180.0,
                    hook_timeout_seconds=10.0,
                    tool_timeout_seconds=120.0,
                ),
            )
        self._safety_policy_cache = policy
        return policy

    def _collect_run_hooks(self) -> list[Hook[Any]]:  # type: ignore[type-arg]
        """Collect optional run hooks configured for this bot service."""
        hooks = self.plugin_integration.collect_hooks()
        obs = self._app_config.observability
        if obs is not None and obs.run_logging:
            from framework.hook.builtin import RunLoggingHook

            level = getattr(logging, obs.level.upper(), logging.INFO)
            hooks.append(
                RunLoggingHook(
                    logger_name="bot.run",
                    level=level,
                    max_content_chars=4000,
                    max_result_chars=4000,
                )
            )
        return hooks

    def _build_hook_runner(self, hooks: list[Hook[Any]]) -> HookRunner[Any]:  # type: ignore[type-arg]
        """Build HookRunner from collected hooks with default HookSpec.

        Default hooks (always present):
          - MaxIterationNotifyHook — notify parent/user when max_iterations hit

        Note: SubagentAutoSendHook is wired separately by _wire_subagent_hooks()
        in AgentCommunicationService, with proper agent_bus and runtime_dir args.
        """
        from framework.hook import HookErrorPolicy, HookRunner, HookSpec
        from framework.hook.notification import MaxIterationNotifyHook

        runner = HookRunner()
        runner.add(HookSpec(hook=MaxIterationNotifyHook(), on_error=HookErrorPolicy.LOG))
        for hook in hooks:
            runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
        return runner

    def _build_control_channel(self) -> InMemoryControlChannel:
        """Build the control channel for control commands."""
        if self.control_channel is None:
            self.control_channel = InMemoryControlChannel()
        return self.control_channel

    def _build_interceptor_chain(self) -> InterceptorChain:
        """Build InterceptorChain with default runtime interceptors.

        Installed interceptors (in order):
          1. ToolResultLimitInterceptor – truncates long tool results
        """
        if self.interceptor_chain is not None:
            return self.interceptor_chain

        chain = InterceptorChain()

        # 1. Tool result overflow
        from framework.tools.overflow.handler import ToolResultOverflowHandler
        from framework.tools.overflow.local import LocalFileToolOverflowStore

        overflow_dir = (
            self.workspace_context.data_dir
            if self.workspace_context is not None
            else self._project_dir
        )
        max_chars = 50_000
        overflow_store = LocalFileToolOverflowStore(workspace=overflow_dir, max_chunk_size=10_000)
        overflow_cleaner = OverflowCleaner(overflow_store)
        overflow_handler = ToolResultOverflowHandler(
            store=overflow_store,
            cleaner=overflow_cleaner,
        )
        chain.add(
            ToolResultLimitInterceptor(
                overflow_handler=overflow_handler,
                max_chars=50_000,
            )
        )

        # Control drain interceptors (consume commands during tool/LLM execution)
        from framework.hook.builtin.control_drain import (
            ControlDrainInterceptor,
            LlmCancelInterceptor,
        )

        chain.add(ControlDrainInterceptor(channel=self.control_channel))
        chain.add(LlmCancelInterceptor(channel=self.control_channel))

        self.interceptor_chain = chain
        return chain

    def _build_main_command_processor(
        self,
        skill_manager: SkillManager | None,
        workspace_ctx: DefaultWorkspaceContext | None = None,
    ) -> SlashCommandProcessor:
        from framework.commands.processor import SlashCommandProcessor

        _ = skill_manager
        if workspace_ctx is not None:
            from framework.commands.handlers import build_default_builtin_handlers
            from framework.workspace.handlers import (
                CdCommandHandler,
                ExitCommandHandler,
                PwdCommandHandler,
            )

            handlers = list(build_default_builtin_handlers())
            handlers.append(CdCommandHandler(workspace_ctx))
            handlers.append(ExitCommandHandler(workspace_ctx))
            handlers.append(PwdCommandHandler(workspace_ctx))
            return SlashCommandProcessor(handlers=handlers)

        return SlashCommandProcessor.default()

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self.mode == "pipeline":
            if self.pipeline:
                await self.pipeline.run()
            return

        # Pool mode: start all pool bridges, then PoolRouter
        await self.input_adapter.start()
        for pool in self._pools.values():
            await pool.broker_bridge.start()
            # Start per-pool experience curator background loop
            if pool.experience_curator is not None:
                # Derive interval from pool config
                interval = 3600
                main_cfg = next(
                    (a for a in pool.config.agents if getattr(a, "role", None) == "main"),
                    None,
                )
                if main_cfg is not None:
                    exp_cfg = getattr(main_cfg, "experience", None)
                    if exp_cfg is not None:
                        interval = getattr(exp_cfg, "curator_interval", 3600)

                pool.experience_curator_task = asyncio.create_task(
                    self._curator_background_loop(pool.experience_curator, interval),
                )
                self._tasks.append(pool.experience_curator_task)
                print(
                    f"   [OK] Pool '{pool.name}': ExperienceCurator started (interval={interval}s)"
                )

        self._start_dream_task()

        self._router_task = asyncio.create_task(self.pool_router.run())
        print(f"[OK] PoolRouter running, {len(self._pools)} pools active")
        await self._shutdown_event.wait()

    # ------------------------------------------------------------------ #
    # Runtime assembly
    # ------------------------------------------------------------------ #

    async def _assemble_runtime(self, hooks: HookRunner[Any] | None = None) -> AgentRuntime:  # type: ignore[type-arg]
        """Build AgentRuntime via framework RuntimeAssembler.

        The only difference between pipeline and pool mode is the hooks source.
        Everything else — classifier, strategy, control, governance — is identical.
        """
        from framework.agents.react.approval import TieredToolApprovalClassifier
        from framework.agents.react.assembler import RuntimeAssembler, RuntimeServicesConfig
        from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig

        # Read agent-level approval config from AppConfig
        main_cfg = self._main_agent_cfg
        approval_cfg = main_cfg.approval if main_cfg else None
        if approval_cfg is not None:
            enabled = approval_cfg.enabled
            tools_approval: dict[str, ToolApprovalConfig] = {
                name: ToolApprovalConfig(allowed_paths=entry.allowed_paths)
                for name, entry in approval_cfg.tools.items()
            }
        else:
            enabled = True
            tools_approval = {}

        approval_config = AgentApprovalConfig(enabled=enabled, tools=tools_approval)

        runtime = await RuntimeAssembler.assemble(
            RuntimeServicesConfig(
                mode="full",
                hooks=hooks,
                interceptors=list(self.interceptor_chain.interceptors)
                if self.interceptor_chain
                else None,
                approval_classifier=TieredToolApprovalClassifier(
                    config=approval_config,
                    argument_matcher=ArgumentMatcher(project_root=self._project_dir),
                ),
                turn_store=self._turn_store,
                control_channel=self.control_channel,
                project_root=self._project_dir,
                governance=create_governance(
                    self._main_memory_cfg, self._app_config.llm.max_tokens
                ),
                safety=self.safety_policy,
            )
        )
        print(
            f"[OK] AgentRuntime built (approval enabled={enabled}, tools={list(tools_approval.keys())})"
        )
        return runtime

    # ------------------------------------------------------------------ #
    # Memory helpers
    # ------------------------------------------------------------------ #

    async def _init_long_term_defaults(
        self,
        _data_dir: Path,
        main_memory_cfg: IOCMemoryConfig | None,
        *,
        memory_system: DefaultMemorySystem | None = None,
    ) -> None:
        """Initialize default long-term memory files if knowledge is enabled.

        Supports both old ``long_term`` config (deprecated) and new ``knowledge``
        config from IOC MemoryConfig.  YAML files that use the new ``knowledge``
        block never populate ``long_term`` (model_post_init only migrates
        long_term → knowledge, not the reverse), so the check must cover both.

        Template paths in config are relative to the project directory.  We
        resolve them to absolute paths before calling ``ensure_defaults`` so
        the knowledge layer finds templates regardless of CWD (critical after
        ``/cd`` which calls ``os.chdir`` to a different directory).
        """
        if main_memory_cfg is None:
            return

        # Check whether knowledge is enabled via either config format
        knowledge_enabled = False
        if main_memory_cfg.long_term is not None and main_memory_cfg.long_term.enabled:
            knowledge_enabled = True
        if main_memory_cfg.knowledge is not None and main_memory_cfg.knowledge.enabled:
            knowledge_enabled = True
        if not knowledge_enabled:
            return

        ms = memory_system or self.memory_system
        if ms is None:
            return

        lt_mgr = ms.knowledge_manager
        if lt_mgr is None:
            return

        # Resolve default_templates_dir to an absolute path so the knowledge
        # layer finds framework templates regardless of CWD.
        raw_template_dir: str | None = None
        if main_memory_cfg.knowledge is not None:
            raw_template_dir = main_memory_cfg.knowledge.default_templates_dir
        if not raw_template_dir and main_memory_cfg.long_term is not None:
            raw_template_dir = main_memory_cfg.long_term.default_templates_dir
        if raw_template_dir:
            abs_template_dir = str((self._project_dir / raw_template_dir).resolve())
            # KnowledgeMemoryConfig is frozen — replace the whole config object
            from dataclasses import replace as _dc_replace

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
                "## 用户画像\n- 首次使用，暂无特定偏好记录\n- 后续对话中会逐渐积累用户习惯和偏好\n"
            ),
            "memory": ("## 相关知识\n- 暂无特定领域知识记录\n- 长期对话中会自动整理和更新\n"),
        }

        ctx = MemoryContext(session_id="default", user_id="default")
        await lt_mgr.ensure_defaults(ctx, defaults)

        print("   [OK] Long-term memory defaults ensured")

    async def _init_maintenance_task(
        self,
        main_memory_cfg: IOCMemoryConfig | None,
    ) -> None:
        """Initialize and start background maintenance via DefaultMemoryMaintenancePolicy."""
        if self.memory_system is None:
            return

        from framework.memory.lifecycle import (
            DefaultArchiveRetentionPolicy,
            DefaultMemoryMaintenancePolicy,
        )

        # Wire archive retention with max_archive_total for FIFO eviction
        archive_retention = None
        if main_memory_cfg is not None and main_memory_cfg.archive is not None:
            max_total = main_memory_cfg.archive.max_archive_total
            if max_total is not None and max_total > 0:
                archive_retention = DefaultArchiveRetentionPolicy(max_archive_total=max_total)

        self._maintenance_policy = DefaultMemoryMaintenancePolicy(
            archive_retention_policy=archive_retention,
        )

        scan_interval = 300
        self._maintenance_task = asyncio.create_task(self._maintenance_loop(scan_interval))
        print(f"   [OK] MaintenanceService started (scan_interval={scan_interval}s)")

    async def _maintenance_loop(self, interval: float) -> None:
        """Background loop for memory maintenance using maintenance policy."""
        while not self._shutdown_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
            if self._shutdown_event.is_set():
                break
            try:
                results = await self._maintenance_policy.scan_once(
                    registry=self.memory_system.store_registry,
                    layers=self.memory_system.layers,
                )
                compacted = [r.scope_key for r in results if r.success]
                if compacted:
                    logger.info("Maintenance scan completed scopes: %s", compacted)
            except Exception:
                logger.exception("Maintenance scan loop error")

    def _init_dream(self) -> None:
        if self._main_memory_cfg is None or self._main_memory_cfg.dream_engine is None:
            return
        if not self._main_memory_cfg.dream_engine.enabled:
            return
        if self.memory_system is None or self.provider is None:
            return
        if (
            self.memory_system.archive_manager is None
            or self.memory_system.knowledge_manager is None
        ):
            return

        dream_cfg = self._main_memory_cfg.dream_engine
        self._dream_interval = dream_cfg.interval
        self.dream_engine = self._build_dream_engine(
            memory_system=self.memory_system,
            dream_cfg=dream_cfg,
        )
        logger.info("DreamEngine initialized, pipeline mode, interval=%ds", self._dream_interval)

    async def _init_pool_dream_engine(self) -> None:
        default_pool = self._pools.get(self._default_pool_name)
        if default_pool is None:
            return
        pool_cfg = default_pool.config
        if pool_cfg.memory is None or pool_cfg.memory.dream_engine is None:
            return
        if not pool_cfg.memory.dream_engine.enabled:
            return
        if default_pool.memory_system is None:
            return
        ms = default_pool.memory_system
        if ms.archive_manager is None or ms.knowledge_manager is None:
            return

        dream_cfg = pool_cfg.memory.dream_engine
        self._dream_interval = dream_cfg.interval
        self.dream_engine = self._build_dream_engine(
            memory_system=ms,
            dream_cfg=dream_cfg,
        )
        logger.info(
            "DreamEngine initialized, pool=%s, interval=%ds",
            self._default_pool_name,
            self._dream_interval,
        )

    def _build_dream_engine(
        self,
        memory_system: DefaultMemorySystem,
        dream_cfg: Any,
    ) -> DreamEngine:
        return DreamEngine(
            history_manager=memory_system.archive_manager,
            long_term_manager=memory_system.knowledge_manager,
            registry=memory_system.store_registry,
            max_consume_per_run=dream_cfg.max_consume_per_run,
            consolidator=memory_system.knowledge_consolidator,
        )

    async def _archive_trigger(self, context: MemoryContext) -> None:
        """Callback invoked after each archive is generated.

        Triggers DreamEngine.run() whenever there are unprocessed archive entries.
        DreamEngine.run() handles its own locking and no-op when idle.
        """
        if self.dream_engine is None:
            return
        try:
            count = await self.memory_system.get_unprocessed_history_count(context)
        except Exception:
            logger.debug("Archive trigger: failed to get unprocessed count", exc_info=True)
            return
        if count > 0:
            logger.info(
                "Archive trigger: %d unprocessed archive(s), running DreamEngine session=%s",
                count,
                context.session_id,
            )
            try:
                await self.dream_engine.run(context)
            except Exception:
                logger.warning("Archive trigger: DreamEngine.run() failed", exc_info=True)

    async def _curator_background_loop(self, curator: object, interval: int) -> None:
        """Periodically run the ExperienceCurator to manage experience lifecycle."""
        logger.info("ExperienceCurator background loop starting, interval=%ds", interval)
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval)
                if self._shutdown_event.is_set():
                    break
                result = await curator.run()
                logger.info(
                    "ExperienceCurator: checked=%d evicted=%d",
                    result.get("checked", 0),
                    result.get("evicted", 0),
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("ExperienceCurator scan error")

    async def _dream_background_loop(self, interval: int = 300) -> None:
        if self.dream_engine is None:
            return
        logger.info(
            "DreamEngine background loop starting, pool=%s interval=%ds",
            self._default_pool_name,
            interval,
        )
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval)
                if self._shutdown_event.is_set():
                    break
                logger.debug(
                    "DreamEngine timer scan, pool=%s interval=%ds",
                    self._default_pool_name,
                    interval,
                )
                processed = await self.dream_engine.scan_all()
                if processed:
                    logger.info(
                        "DreamEngine processed %d scope(s), pool=%s",
                        len(processed),
                        self._default_pool_name,
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("DreamEngine background loop error")

    async def stop(self) -> None:
        logger.info(
            "BotService.stop() called — shutdown trigger:\n%s",
            "".join(traceback.format_stack()[-5:-1]),
        )
        self._shutdown_event.set()
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
        if self._dream_task is not None:
            self._dream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dream_task
        if hasattr(self, "_router_task") and self._router_task:
            self._router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._router_task
        if self.mcp_manager is not None:
            with contextlib.suppress(BaseException):
                await self.mcp_manager.disconnect_all()
        # Shut down all pools in one pass
        for pi in self._pools.values():
            if pi.experience_curator_task is not None:
                pi.experience_curator_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pi.experience_curator_task
            if pi.mcp_manager is not None:
                with contextlib.suppress(BaseException):
                    await pi.mcp_manager.disconnect_all()
            with contextlib.suppress(BaseException):
                await pi.pool.shutdown_all()
            with contextlib.suppress(BaseException):
                await pi.broker_bridge.stop()
        # Close all terminal sessions
        await self._close_all_terminals(suppress_errors=True)
        with contextlib.suppress(BaseException):
            await self.input_adapter.stop()
        if self._trace_writer is not None:
            with contextlib.suppress(BaseException):
                self._trace_writer.close()
        if self.broker:
            with contextlib.suppress(BaseException):
                await self.broker.stop()
