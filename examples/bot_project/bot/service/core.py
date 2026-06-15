"""BotService core — generic bot orchestration for any InputAdapter/OutputAdapter pair.

Runtime: AgentPool with resident agents, BrokerBridgeService routes messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from framework.core.experience.curator import ExperienceCurator
from framework.workspace import DefaultWorkspaceContext, WorkspaceSwitchCallback

if TYPE_CHECKING:
    from framework.commands.processor import SlashCommandProcessor
    from framework.core.experience.manager import ExperienceManager
    from framework.hook.builtin.experience_review import ExperienceReviewHook
    from framework.memory.pruned.manager import PrunedManager
    from framework.runtime.codec import RuntimeStateCodecRegistry
    from framework.runtime.store import (
        JsonFileRuntimeCommandStore,
        JsonFileTurnStateStore,
        TurnStateStore,
    )

from bot.plugins.integration import PluginIntegration
from bot.utils.config_loader import ConfigLoader
from framework import (
    AgentPipeline,
    LLMProvider,
)
from framework.approval.ui import IMUserInterface
from framework.control.channel import InMemoryControlChannel
from framework.core.emitter import ContentEmitter
from framework.core.session_registry import SessionRegistry
from framework.core.session_store import SessionStore
from framework.core.llm_struct import (
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from framework.core.skills import (
    SkillManager,
)
from framework.hook.abc import Hook
from framework.hook.runner import HookRunner
from framework.interceptor.builtin import (
    ToolResultLimitInterceptor,
)
from framework.interceptor.chain import InterceptorChain
from framework.ioc.configs.agent import AgentConfig as IOCAgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.memory import DreamEngineConfig
from framework.ioc.configs.memory import MemoryConfig as IOCMemoryConfig
from framework.ioc.factories.memory import create_memory
from framework.memory.consolidation.dream_engine import DreamEngine
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.system import MemorySystemContextManager
from framework.messaging.broker_bridge import (
    BrokerBridgeService,
)
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    CommunicationTracker,
    SessionRetentionPolicy,
)
from framework.multi_agent.bus import AgentMessageBus, LocalAgentMessageBus
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.pipeline.adapters import InputAdapter, OutputAdapter
from framework.tools.overflow.cleaner import OverflowCleaner

from .builders import AgentBuilderMixin
from .pool_builder import create_pool, ensure_long_term_defaults
from .pool_instance import PoolInstance
from .pool_router import PoolRouter, PoolSessionStore

logger = logging.getLogger(__name__)

# ── Workspace data subdirectory constants ──────────────────────────────
# Used by _ws_* helpers so the layout is defined once and shared between
# initial pool creation and cd/exit rebuilds.

_SUBDIR_MEMORY = "memory"
_SUBDIR_RUNTIME = "runtime_state"
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


def _find_main_agent_name(pool_inst: Any) -> str:
    """Extract main agent name from a PoolInstance's config."""
    agents = getattr(pool_inst.config, "agents", []) or []
    for a in agents:
        if getattr(a, "role", None) == "main":
            return a.name
    return "main"


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

    Runtime: AgentPool with MessageBroker routing.
    Accepts an IOC AppConfig object as the single source of truth.
    """

    def __init__(
        self,
        config_dir: Path,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Callable[[str], ContentEmitter],
        *,
        app_config: AppConfig | None = None,
        # ── Injection points for pool creation ──
        output_adapter_factory: Callable[[], OutputAdapter] | None = None,
        on_subagent_created: Callable[[str, str], Awaitable[None]] | None = None,
        session_registry: SessionRegistry | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.config_loader = ConfigLoader(config_dir)
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory
        self._app_config = app_config
        self._output_adapter_factory = output_adapter_factory
        self._on_subagent_created = on_subagent_created

        # SessionInfo registry/store — injected into every pool so subagent
        # sessions are registered with their parent_session_id and resolvable
        # at dispatch time (SubagentAutoSendHook needs parent to notify).
        self._session_registry: SessionRegistry | None = session_registry
        self._session_store: SessionStore | None = session_store

        # Shared components
        self.broker_bridge: BrokerBridgeService | None = None
        self.agent_bus: AgentMessageBus | None = None
        self.communication_tracker: CommunicationTracker | None = None
        self.broker: InMemoryMessageBroker | None = None
        self.inbox_server: LocalFileInboxServer | None = None
        self.inbox_producer: InboxProducer | None = None
        self.inbox_consumer: InboxConsumer | None = None

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

        # Approval
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
        print("=" * 60)

        # 1. Load config (IOC AppConfig is the only source of truth)
        if self._app_config is None:
            self._app_config = self._load_app_config()
        assert self._app_config is not None, "AppConfig must be loaded before initialize"
        print(f"[OK] Config loaded ({len(self._app_config.agents)} agents via IOC)")

        # 1.1 Warn if LLM credentials are missing — the service can still start,
        # but chat will fail until the user runs ``modexbot config``.
        # Delegates to LLMConfig.missing_required_fields() so the check lives
        # in the config model, not duplicated here.
        default_pool_cfg = self._app_config.pools.get(self._app_config.multi_agent.default_pool)
        if default_pool_cfg is not None:
            missing_llm = default_pool_cfg.llm.missing_required_fields()
            if missing_llm:
                print(
                    f"[WARNING] LLM config incomplete: {', '.join(missing_llm)}. "
                    "Run 'modexbot config' to set them. Chat will fail until configured."
                )

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

        # 2.5 Build shared interceptor chain
        self.interceptor_chain = self._build_interceptor_chain()

        # 2.6 Build shared control channel
        self.control_channel = self._build_control_channel()

        # Plugins are OFF by default in pool mode.
        self.plugin_integration = PluginIntegration(config={"enabled": False})
        await self._initialize_pool()
        self._print_pool_info()

        print("=" * 60)

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
        self._im_ui = IMUserInterface(
            output_adapter=self.output_adapter,
            channel=self.control_channel,
        )

        # 4. Shared infra: Hooks & Interceptors
        shared_hooks = self._collect_run_hooks()

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
                inbox_consumer=self.inbox_consumer,
                agent_bus=self.agent_bus,
                output_adapter=self.output_adapter,
                safety=self.safety_policy,
                retention=retention,
                comm_tracker=self.communication_tracker,
                im_ui=self._im_ui,
                shared_hooks=shared_hooks,
                shared_hook_runner=shared_hook_runner,
                shared_interceptor_chain=shared_interceptor_chain,
                control_channel=self.control_channel,
                command_processor=self.command_processor,
                workspace_context=self.workspace_context,
                emitter_factory=self.emitter_factory,
                # ── Injection points ──
                output_adapter_factory=self._output_adapter_factory,
                on_subagent_created=self._on_subagent_created,
                session_registry=self._session_registry,
                session_store=self._session_store,
            )
            print(f"[OK] Pool '{pool_name}' created")

        # 7. PoolRouter
        session_store = PoolSessionStore(data_dir=data_dir)
        self.pool_router = PoolRouter(
            input_adapter=self.input_adapter,
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

        检查所有 pool 的 agent 是否有正在运行的 agent turn（含 subagent）。

        Returns:
            True 表示有活跃 agent，应拒绝 cd。
        """

        def check() -> bool:
            return any(pool_inst.pool.has_active_sessions() for pool_inst in self._pools.values())

        return check

    # ------------------------------------------------------------------ #
    # Workspace switch callbacks (registered in initialize)
    # ------------------------------------------------------------------ #

    async def _on_ws_stop_and_rebuild(self, _old_dir: Path, new_dir: Path) -> None:
        """① Stop background + rebuild stores (atomic)."""
        await self._stop_background_tasks()
        self._clear_subagent_caches()
        await self._rebuild_pool_memory(new_dir)
        self._update_communication_paths(new_dir)
        await self._rebuild_shared_infrastructure(new_dir)
        self._rebuild_session_stores(new_dir)

    async def _on_ws_terminal_reset(self, _old_dir: Path, _new_dir: Path) -> None:
        """② Close all terminal sessions."""
        await self._close_all_terminals(suppress_errors=True)

    # ------------------------------------------------------------------ #
    # Workspace switch helpers
    # ------------------------------------------------------------------ #

    async def _close_all_terminals(self, *, suppress_errors: bool = True) -> None:
        """Close every terminal session across all pools.

        Used by both workspace-switch callbacks and BotService.stop().
        """
        for mgr in [pi.terminal_manager for pi in self._pools.values()]:
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
        # Clear injection queues on all pool agent pipelines
        for pi in self._pools.values():
            for ai in pi.pool._agents.values():
                if ai.pipeline is not None:
                    ai.pipeline._injection_queues.clear()

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

        return new_memory, new_turn_store, new_cmd_store

    async def _rebuild_experience(self, new_data_dir: Path) -> None:
        """Rebuild experience paths on workspace switch.

        Two subsystems to update:

        1. Injection-side ExperienceManager on each pool's context_manager
           (feeds EXPERIENCE.md content into the system prompt).
        2. Review hook + curator on each PoolInstance (the hook runs
           after-turn reviews; the curator evicts excess experiences).
           Both use ``lambda: dir_ref[0]`` internally, so updating
           ``dir_ref[0]`` is sufficient.
        """
        # ── 1. Rebuild injection-side ExperienceManager ──
        from framework.core.experience.manager import ExperienceManager
        from framework.core.experience.source import FileExperienceSource

        def _new_manager(pool_name: str, agent_name: str) -> ExperienceManager:
            exp_dir = BotService._ws_experience(
                new_data_dir, pool_name=pool_name, agent_name=agent_name
            )
            exp_dir.mkdir(parents=True, exist_ok=True)
            return ExperienceManager(source=FileExperienceSource(directories=[exp_dir]))

        for pool_inst in self._pools.values():
            if pool_inst.context_manager._experience_manager is None:
                continue
            main_agent_name = _find_main_agent_name(pool_inst)
            mgr = _new_manager(pool_inst.name, main_agent_name)
            pool_inst.context_manager._experience_manager = mgr
            logger.info(
                "Pool '%s': injection ExperienceManager rebuilt → %s",
                pool_inst.name,
                BotService._ws_experience(
                    new_data_dir, pool_name=pool_inst.name, agent_name=main_agent_name
                ),
            )

        print(f"[OK] Experience injection rebuilt for workspace: {new_data_dir}")

        # ── 2. Update review hook + curator dir refs ──
        for pool_inst in self._pools.values():
            if pool_inst.experience_dir_ref is None:
                continue
            main_agent_name = _find_main_agent_name(pool_inst)
            new_exp_dir = BotService._ws_experience(
                new_data_dir, pool_name=pool_inst.name, agent_name=main_agent_name
            )
            new_exp_dir.mkdir(parents=True, exist_ok=True)
            pool_inst.experience_dir_ref[0] = new_exp_dir
            logger.info(
                "Pool '%s': experience dir ref updated → %s",
                pool_inst.name, new_exp_dir,
            )

        if any(p.experience_dir_ref is not None for p in self._pools.values()):
            print(f"[OK] Experience hook/curator paths updated for workspace: {new_data_dir}")

    async def _rebuild_shared_infrastructure(self, new_data_dir: Path) -> None:
        """更新共享基础设施：inbox + approval + overflow + dream。"""
        # Inbox
        inbox_dir = self._ws_inbox(new_data_dir)
        inbox_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_server._workspace = inbox_dir
        # Sync delivered-id tracker workspace so dedup writes to the correct dir
        if hasattr(self.inbox_server, "_tracker") and hasattr(
            self.inbox_server._tracker, "_workspace"
        ):
            self.inbox_server._tracker._workspace = inbox_dir
        # Overflow store
        self._rebuild_overflow_store(new_data_dir)
        # Dream engine + task
        await self._init_pool_dream_engine()
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

    def _rebuild_session_stores(self, new_data_dir: Path) -> None:
        """Rebuild session stores (transcript, relation, pool routing) for workspace switch.

        Called from ``_on_ws_stop_and_rebuild``.  Each workspace maintains its
        own ``.modex/sessions/`` and ``.modex/pool_sessions/`` directories.

        Subclasses (e.g. WebUIService) override ``update_session_stores`` to
        rebuild their specific session-related components.
        """
        # 1. PoolSessionStore — rebuild to new workspace
        new_session_store = PoolSessionStore(data_dir=new_data_dir)
        if self.pool_router is not None:
            self.pool_router._session_store = new_session_store

        # 2. Transcript + relation stores — delegate to subclass
        self.update_session_stores(new_data_dir)

        print(f"[OK] Session stores rebuilt for workspace: {new_data_dir}")

    def update_session_stores(self, new_data_dir: Path) -> None:
        """Rebuild subclass-specific session stores (no-op base, overridden by WebUIService).

        Args:
            new_data_dir: The new workspace data directory (``{workspace}/.modex/``).
        """
        pass
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
        # Start all pool bridges, then PoolRouter
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
    # Memory helpers
    # ------------------------------------------------------------------ #

    async def _init_long_term_defaults(
        self,
        _data_dir: Path,
        main_memory_cfg: IOCMemoryConfig | None,
        *,
        memory_system: DefaultMemorySystem,
    ) -> None:
        """Initialize default long-term memory files if knowledge is enabled.

        Delegates to the shared pool-builder helper so initial creation and
        workspace-switch rebuilds use the same absolute-template logic.
        """
        await ensure_long_term_defaults(
            self._project_dir,
            main_memory_cfg,
            memory_system,
        )

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
        dream_cfg: DreamEngineConfig,
    ) -> DreamEngine:
        return DreamEngine(
            history_manager=memory_system.archive_manager,
            long_term_manager=memory_system.knowledge_manager,
            registry=memory_system.store_registry,
            max_consume_per_run=dream_cfg.max_consume_per_run,
            consolidator=memory_system.knowledge_consolidator,
        )

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
        if self.broker:
            with contextlib.suppress(BaseException):
                await self.broker.stop()
