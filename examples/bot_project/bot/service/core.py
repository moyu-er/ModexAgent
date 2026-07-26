"""BotService core — generic bot orchestration for any InputAdapter/OutputAdapter pair.

Runtime: AgentPool with resident agents, BrokerBridgeService routes messages.

Workspace: BotService owns a multi-live workspace stack
(:func:`bot.workspace.wiring.build_workspace_stack`). Per-workspace
data (memory / runtime stores / experience) + per-workspace broker/inbox/bus
live on each workspace's :class:`PoolWorkspaceResources` (R) and are resolved
at turn time — never cached on PoolInstance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import ValidationError

if TYPE_CHECKING:
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

from bot.plugins.integration import PluginIntegration
from bot.service.errors import BotServiceShutdownIncompleteError
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider
from bot.utils.config_loader import ConfigLoader
from bot.workspace.wiring import build_single_workspace_stack, build_workspace_stack
from modex_agent import (
    LLMProvider,
)
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import (
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.hook.abc import Hook
from modex_agent.hook.runner import HookRunner
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter
from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR, WORKSPACE_STATE_DB

from .builders import AgentBuilderMixin, resolve_system_prompt

logger = logging.getLogger(__name__)


class BotService(AgentBuilderMixin):
    """Generic bot service supporting arbitrary InputAdapter/OutputAdapter pairs.

    Can be used for QQ, Discord, Feishu, DingTalk, Telegram, CLI, etc.
    Just provide the corresponding adapters and an Emitter factory.

    Runtime: AgentPool with MessageBroker routing.
    Accepts an IOC AppConfig object as the single source of truth.
    """

    # Whether this service runs the WebUI. Set True by WebUIService; controls
    # workspace-level transcript/session_index store wiring. Read by _is_webui().
    webui: bool = False

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
        # Multi-model config (config/model.yml 的 models: 块) + per-turn choice
        # registry。_load_app_config 解析并缓存；_build_*_provider / wiring 读取。
        self._bot_model_config: BotModelConfig | None = None
        self._model_choice_registry: ModelChoiceRegistry | None = None
        if app_config is not None:
            # 子类（WebUIService/QQBotService）预加载了 AppConfig 传入——立即做
            # bot 层 model.yml 后处理，确保 _bot_model_config 在 initialize()/
            # start() 与 server/pipeline 装配读取前就已就位（spec B3）。app_config
            # 为 None 时，initialize() 经 _load_app_config 加载并应用同样的后处理。
            self._apply_bot_model_config(app_config)
        self._output_adapter_factory = output_adapter_factory
        self._on_subagent_created = on_subagent_created

        # SessionInfo registry/store — injected into every pool so subagent
        # sessions are registered with their parent_session_id and resolvable
        # at dispatch time (SubagentAutoSendHook needs parent to notify).
        self._session_registry: SessionRegistry | None = session_registry
        self._session_store: SessionStore | None = session_store

        # Multi-live workspace stack (built in initialize). Owns the registry,
        # conversation map, resolver, controller, dispatcher, factory. The
        # controller is the per-conversation WorkspaceControlPort passed to
        # the cd/exit/pwd handlers; ``workspace_context`` is a compat alias.
        self.workspace_stack: Any = None
        self.workspace_context: Any = None
        # Eagerly materialized home resources (the default workspace). Holds
        # the home pools + router; BotService.start/stop operate on these for
        # v1 (home-only materialization).
        self._home_resources: Any = None

        # Multi-pool view of the HOME workspace (compat for _print_pool_info
        # and any direct readers). Per-workspace pools live on each R.
        self._pools: dict[str, PoolInstance] = {}
        self.pool_router: Any = None
        # Service-level session→pool mapping store. Shared across workspaces so
        # a mapping written by the WebUI (or ResolvePoolStage) is visible to the
        # pool_router of whatever workspace ultimately dispatches the message.
        self._pool_session_store: PoolRoutingStore | None = None
        self.plugin_integration: PluginIntegration | None = None

        # Shared MCP connection registry (ADR-0017 Task 5a). Service-scoped,
        # concurrent, dedup-by-config-hash. Built in initialize() when the
        # ``sharedRegistry`` flag (config/mcp/registry.json, default ON) is set;
        # pools then acquire SharedMcpBackend facades via registry.acquire.
        # None when the flag is off (or registry absent) → legacy per-pool path.
        self._mcp_registry: McpConnectionRegistry | None = None

        # Execution-strategy registry (ADR-0025, ticket 3). Built in
        # initialize() with the shipped strategies (react in ticket 3;
        # external_coding added in ticket 4). Threaded through wiring.py into
        # create_pool so react pools are assembled via
        # ReactExecutionStrategy.assemble() instead of inline _build_* calls.
        self._strategy_registry: Any = None

        # T26: Registry-level SQLite persistence manager. Opened at initialize()
        # (before workspace materialize), closed at stop() AFTER evict_all (the
        # registry DB is the last-to-close persistence layer). None when
        # backend is FILE or initialize() hasn't run yet.
        self._registry_persistence: RegistryPersistenceManager | None = None
        self._home_persistence: WorkspacePersistenceManager | None = None


        # Maintenance
        self._maintenance_task: asyncio.Task | None = None

        # Runtime codec (kept for reference; stores now live on the workspace)
        self._runtime_codec_registry: RuntimeStateCodecRegistry | None = None

        # Control plane (shared across workspaces).
        self.control_channel: InMemoryControlChannel | None = None
        self.command_processor: SlashCommandProcessor | None = None
        self._safety_policy_cache: RuntimeSafetyPolicy | None = None

        # Approval
        self._default_provider: LLMProvider | None = None

        # Cached system prompts per pool (resolved once, reused across switches)
        self._system_prompt_cache: dict[str, str] = {}

        # Router task (the workspace dispatcher loop)
        self._router_task: asyncio.Task | None = None

        # Runtime control
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def _default_pool_name(self) -> str | None:
        return None

    def _is_webui(self) -> bool:
        """Whether this service runs the WebUI (class attribute ``webui``)."""
        return self.webui

    def _build_default_provider(self) -> LLMProvider | None:
        """Build the default pool's LLM provider (memory/summarizer layer).

        统一用 BotModelProvider：未设置 ContextVar 时（memory summarizer、后台任务）
        自动落到默认模型。

        Returns ``None`` when ``model.yml`` is absent or unconfigured — the bot
        boots without a model so the user can configure one via the WebUI
        (Settings → Models) or ``modexbot config`` after first start. Chat
        turns will fail until a provider is configured, but the WebUI itself
        is fully usable. Downstream consumers (``build_pool_data``,
        ``create_memory``) already accept ``provider=None``.
        """
        if self._bot_model_config is None:
            return None
        return BotModelProvider(self._bot_model_config)

    def _load_app_config(self) -> AppConfig:
        """Load IOC AppConfig from bot_config.yml + 多模型后处理。

        框架 AppConfig.from_yaml 不再注入 pool_cfg.llm；模型配置完全由
        BotModelConfig / BotModelProvider 管理。
        """
        app_config = AppConfig.from_yaml(self.config_dir / "bot_config.yml")
        self._apply_bot_model_config(app_config)
        return app_config

    def _load_bot_model_config_for_listing(self) -> BotModelConfig | None:
        """Re-read config/model.yml fresh for GET /api/models (live refresh).

        The selector must reflect CLI edits (``modexbot model``) without a server
        restart, so the endpoint re-reads the file on each request rather than
        serving the startup-cached ``_bot_model_config``. Returns None on a
        missing or unparseable file (endpoint then reports an empty list).
        Runtime routing still uses the cached config and requires restart.
        """
        model_yml = self.config_dir / "model.yml"
        if not model_yml.exists():
            return None
        try:
            return BotModelConfig.from_yaml(model_yml)
        except (ValidationError, yaml.YAMLError, OSError):
            logger.exception("model.yml parse failed for /api/models listing")
            return None

    def _apply_bot_model_config(self, app_config: AppConfig) -> None:
        """Bot 层后处理（spec B3）：解析 model.yml 的 models: 块，缓存
        BotModelConfig。无论 AppConfig 由本服务加载还是子类预加载传入，都
        必须运行——_bot_model_config 是后续 provider/wiring 的依赖。

        PoolSpec 不再携带 llm；模型配置由 BotModelConfig / BotModelProvider
        独立管理。max_context_tokens 由 wiring 层注入 PoolAssemblyDeps.memory。

        model.yml 缺失时（如框架单测用 config_dir=Path('.') + 合成 app_config，
        或首次部署尚未运行 ``modexbot config``）静默跳过，_bot_model_config
        留 None；``_build_default_provider`` 随之返回 None，bot 以无模型状态启动，
        供用户在 WebUI 里完成首次配置。
        """
        model_yml = self.config_dir / "model.yml"
        if not model_yml.exists():
            return
        model_cfg = BotModelConfig.from_yaml(model_yml)
        self._bot_model_config = model_cfg

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #

    # Workspace layout is owned by WorkspacePaths (Unit A); per-pool data by
    # Workspace.build_pool_data (Unit D). No workspace path math lives in
    # BotService anymore.

    @property
    def _project_dir(self) -> Path:
        """Project root directory (where bot_service.py lives).

        resolve() ensures the path is absolute even when __file__ is relative,
        which can happen when running via python examples/bot_project/bot_service.py
        from a different CWD (common in production deployments).
        """
        return Path(__file__).resolve().parent.parent.parent

    @property
    def project_dir(self) -> Path:
        """Public accessor for the project root directory."""
        return self._project_dir

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

        # ADR-0027 T8: register cooperative SIGTERM/SIGINT handlers that
        # run atexit cleanup (killing any live ``opencode serve``
        # subprocesses) before exit. Idempotent — safe to call every boot.
        from modex_agent.agents.external_coding.os_layer import (
            register_signal_handlers,
        )

        register_signal_handlers()

        # 1. Load config (IOC AppConfig is the only source of truth)
        if self._app_config is None:
            self._app_config = self._load_app_config()
        assert self._app_config is not None, "AppConfig must be loaded before initialize"
        from modex_agent.multi_agent.pool_config import PoolStore

        pool_store = PoolStore(base_dir=self._project_dir)
        print(f"[OK] Config loaded ({len(pool_store.list_pools())} pools)")

        # Shared MCP connection registry (ADR-0017 Task 5a). Service-scoped,
        # concurrent, dedup-by-config-hash. When the sharedRegistry flag is on
        # (default), all pools/agents/workspaces share one subprocess per
        # configured server and switching/first-open of workspaces is free.
        # start_connecting fires all supervisors now so connections are READY
        # by the time the home workspace's pools build (which call
        # _load_agent_mcp_tools → registry.acquire and find them instantly).
        # Set ``sharedRegistry: false`` in config/mcp/registry.json to disable
        # and fall back to today's per-pool MCPClientManager path.
        from bot.config.mcp_registry import read_registry, read_shared_registry_flag
        from modex_agent.ioc.configs.app import _resolve_env_in
        from modex_agent.tools.mcp.injector import JsonFileMCPTransportInjector
        from modex_agent.tools.mcp.registry import McpConnectionRegistry

        mcp_registry_path = self._project_dir / "config" / "mcp" / "registry.json"
        if read_shared_registry_flag(mcp_registry_path):
            raw_servers = read_registry(mcp_registry_path)
            if raw_servers:
                # ${ENV} interpolation MUST happen before the registry hashes
                # and connects, else tokens like ${MY_TOKEN} reach the
                # subprocess literally.
                servers = _resolve_env_in(raw_servers)
                self._mcp_registry = McpConnectionRegistry(
                    servers=servers,
                    injector=JsonFileMCPTransportInjector(),
                )
                self._mcp_registry.start_connecting(list(servers.keys()))
                print(
                    f"[OK] Shared MCP registry: {len(servers)} server(s) connecting concurrently"
                )
            else:
                self._mcp_registry = None
        else:
            self._mcp_registry = None

        # The shared MCP registry (if started above) spawned long-lived
        # supervisor tasks holding real MCP subprocesses. If any later step of
        # initialize() raises, those supervisors would leak until process exit.
        # Guard the post-start_connecting tail so the registry is shut down
        # (subprocesses closed in-task) before the exception propagates.
        try:
            # 1.1 Warn if the model is not configured — the service still
            # starts (WebUI usable, config editable via Settings → Models),
            # but chat turns will fail until the user runs ``modexbot config``
            # or sets a provider in the WebUI.
            if self._bot_model_config is None:
                logger.warning(
                    "No model configured (config/model.yml missing). "
                    "The bot is running but chat will fail until you configure a model. "
                    "Open the WebUI → Settings → Models, or run 'modexbot config'."
                )
            else:
                default_resolved = self._bot_model_config.default_resolved()
                missing_llm: list[str] = []
                if not default_resolved.provider.api_key:
                    missing_llm.append("api_key")
                if not default_resolved.provider.base_url:
                    missing_llm.append("base_url")
                if missing_llm:
                    logger.warning(
                        "LLM config incomplete: %s. Run 'modexbot config' to set them. "
                        "Chat will fail until configured.",
                        ", ".join(missing_llm),
                    )

            # 1.5 Build the default LLM provider + the workspace stack.
            # Branch on workspace.enabled: False -> single-home stack (no /cd);
            # True -> full multi-live stack.
            self._model_choice_registry = ModelChoiceRegistry()
            self._default_provider = self._build_default_provider()
            self.control_channel = self._build_control_channel()
            self.command_processor = self._build_main_command_processor()
            self.plugin_integration = PluginIntegration(config={"enabled": False})

            # ADR-0025 ticket 3: build the execution-strategy registry with
            # the shipped react strategy. Threaded through wiring.py into
            # create_pool so react pools are assembled via
            # ReactExecutionStrategy.assemble() instead of inline _build_*.
            # ADR-0025 ticket 4: register ExternalCodingExecutionStrategy so
            # external_coding pools are assembled via strategy.assemble()
            # (provider-availability gate + external_coding_deps build).
            from bot.service.external_coding_strategy import (
                ExternalCodingExecutionStrategy,
            )
            from bot.service.react_strategy import ReactExecutionStrategy
            from modex_agent.multi_agent.execution_strategy import (
                ExecutionStrategyRegistry,
            )

            self._strategy_registry = ExecutionStrategyRegistry()
            self._strategy_registry.register(ReactExecutionStrategy())
            self._strategy_registry.register(ExternalCodingExecutionStrategy())

            # T26: open the registry DB BEFORE workspace materialization so the
            # registry store is ready when workspaces start using it. The
            # registry DB closes LAST at stop() (after all workspaces evicted).
            if self._app_config.persistence.backend is PersistenceBackend.SQLITE:
                from modex_agent.persistence.managers import (
                    RegistryPersistenceManager,
                    WorkspacePersistenceManager,
                )

                registry_db_path = (
                    self._project_dir
                    / self._app_config.paths.data_dir_name
                    / RESERVED_GLOBAL_DIR
                    / WORKSPACE_STATE_DB
                )
                self._registry_persistence = RegistryPersistenceManager(registry_db_path)
                await self._registry_persistence.open()

                home_db_path = (
                    self._project_dir
                    / self._app_config.paths.data_dir_name
                    / WORKSPACE_STATE_DB
                )
                self._home_persistence = WorkspacePersistenceManager(home_db_path)
                await self._home_persistence.open()

            from bot.service.builders import build_pool_routing_store

            home_data_dir = self._project_dir / self._app_config.paths.data_dir_name
            self._pool_session_store = build_pool_routing_store(
                self._app_config,
                self._home_persistence,
                data_dir=home_data_dir,
                db_path=home_data_dir / WORKSPACE_STATE_DB,
            )

            if self._app_config.workspace.enabled:
                self.workspace_stack = build_workspace_stack(
                    self, data_dir_name=self._app_config.paths.data_dir_name
                )
            else:
                self.workspace_stack = build_single_workspace_stack(
                    self, data_dir_name=self._app_config.paths.data_dir_name
                )
            self.workspace_context = self.workspace_stack.controller
            await self.workspace_stack.registry.initialize()

            # Eagerly materialize the HOME workspace so its pools/router are live
            # for BotService.start/stop (v1 = home-only materialization). The
            # dispatcher lazily materializes other workspaces on first turn.
            self._home_resources = await self.workspace_stack.registry.materialize(
                self.workspace_stack.registry.home_context
            )
            self._pools = self._home_resources.pools
            self.pool_router = self._home_resources.pool_router
            self._print_pool_info()

            print("=" * 60)
        except BaseException as initialization_error:
            resources_evicted = True
            if self.workspace_stack is not None:
                try:
                    resources_evicted = await self.workspace_stack.registry.evict_all()
                except BaseException as cleanup_error:
                    resources_evicted = False
                    initialization_error.add_note(
                        "workspace eviction failed during initialization cleanup: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                if not resources_evicted:
                    initialization_error.add_note(
                        "workspace eviction incomplete during initialization cleanup; "
                        "shared persistence retained for retry"
                    )
            if self._mcp_registry is not None:
                with contextlib.suppress(BaseException):
                    await self._mcp_registry.shutdown()
            if resources_evicted and self._pool_session_store is not None:
                with contextlib.suppress(BaseException):
                    self._pool_session_store.close()
                self._pool_session_store = None
            if resources_evicted and self._home_persistence is not None:
                with contextlib.suppress(BaseException):
                    await self._home_persistence.close()
                self._home_persistence = None
            if resources_evicted and self._registry_persistence is not None:
                with contextlib.suppress(BaseException):
                    await self._registry_persistence.close()
                self._registry_persistence = None
            raise

    def _print_pool_info(self) -> None:
        """Display pool configuration summary."""
        print(f"\n[INFO] Pools: {list(self._pools.keys())}")
        for name, pi in self._pools.items():
            print(f"   {name}: {pi.main_agent_name} + {pi.subagent_count} subagents")
        print(f"[INFO] Switch commands: /{' /'.join(self._pools.keys())}")
        print(f"[INFO] Default pool: {self._default_pool_name}")

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # System-prompt resolution (per-pool, cached)
    # ------------------------------------------------------------------ #

    def _system_prompt_for(self, name: str) -> str:
        """Resolve the pool's main-agent system prompt (cached per pool)."""
        cached = self._system_prompt_cache.get(name)
        if cached is not None:
            return cached
        from modex_agent.multi_agent.pool_config import PoolStore

        pool_spec = PoolStore(base_dir=self._project_dir).read_pool(name)
        prompt = resolve_system_prompt(pool_spec.main.agent_name, self._project_dir)
        self._system_prompt_cache[name] = prompt
        return prompt

    # ------------------------------------------------------------------ #
    # Workspace helpers
    # ------------------------------------------------------------------ #

    async def _close_all_terminals(self, *, suppress_errors: bool = True) -> None:
        """Close every terminal session across all pools concurrently.

        Used by workspace deactivate and BotService.stop().
        """
        tasks: list[asyncio.Task[None]] = []
        for mgr in [pi.terminal_manager for pi in self._pools.values()]:
            if mgr is None:
                continue
            for name in list(mgr.list_names()):
                tasks.append(
                    asyncio.create_task(
                        self._close_terminal(mgr, name, suppress_errors=suppress_errors)
                    )
                )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _close_terminal(
        self, mgr: Any, name: str, *, suppress_errors: bool
    ) -> None:
        """Close a single terminal session, optionally swallowing errors."""
        try:
            await mgr.close(name)
        except BaseException:
            if not suppress_errors:
                raise

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
                    agent_run_timeout_seconds=420.0,
                    hook_timeout_seconds=10.0,
                ),
            )
        self._safety_policy_cache = policy
        return policy

    def _collect_run_hooks(self) -> list[Hook[Any]]:  # type: ignore[type-arg]
        """Collect optional run hooks configured for this bot service."""
        hooks = self.plugin_integration.collect_hooks()
        obs = self._app_config.observability
        if obs is not None and obs.run_logging:
            from modex_agent.hook.builtin import RunLoggingHook

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
        from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
        from modex_agent.hook.notification import MaxIterationNotifyHook

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

    def _build_main_command_processor(self) -> SlashCommandProcessor:
        """Build the slash command processor.

        Wires the default builtin handlers.  Workspace commands (/cd,
        /exit, /pwd) are handled directly by the IM input pipeline
        (``EnvironmentControlStage``) so they are removed from the
        processor — this avoids self-blocking where the command's own
        dispatch would appear as an "active agent" in pool mode.
        """
        from modex_agent.commands.handlers import build_default_builtin_handlers
        from modex_agent.commands.processor import SlashCommandProcessor

        return SlashCommandProcessor(handlers=list(build_default_builtin_handlers()))

    def _iter_workspace_resources(self) -> Iterator[Any]:
        """Yield all materialized workspace resource bundles.

        Used by the control-filter session checker/turn-uuid getter to locate
        the AgentPipeline responsible for a given session across workspaces.
        """
        yield self._home_resources
        if self.workspace_stack is not None:
            yield from self.workspace_stack.registry.iter_materialized_resources()

    def _is_session_active(self, session_id: str) -> bool:
        """Return True if *session_id* has a running turn in any workspace pool."""
        for resources in self._iter_workspace_resources():
            for pi in resources.pools.values():
                for inst in pi.pool.iter_instances():
                    if inst.pipeline is not None and inst.pipeline.is_session_active(session_id):
                        return True
        return False

    def _get_active_turn_uuid(self, session_id: str) -> str | None:
        """Return the current turn UUID for *session_id*, or None if not running."""
        for resources in self._iter_workspace_resources():
            for pi in resources.pools.values():
                for inst in pi.pool.iter_instances():
                    if inst.pipeline is None:
                        continue
                    uuid = inst.pipeline.get_active_turn_uuid(session_id)
                    if uuid is not None:
                        return uuid
        return None

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        # Start the input adapter, then the workspace dispatcher (resolves
        # the conversation's workspace per message and routes into that
        # workspace's pool_router).
        # Broker bridges, dream + curator background tasks are workspace-scoped
        # and are started inside build_resources when EACH workspace
        # materializes — home and non-home alike, so every switched-to /
        # newly-created workspace is fully wired (not just home).

        # Wire the shared control filter BEFORE the input adapter starts so
        # IM /stop (and the WebUI pause button, which reuses /stop) actually
        # push CANCEL_TURN through InMemoryControlChannel. Idempotent if a
        # subclass (e.g. WebUIService) already wired it earlier.
        self.input_adapter.configure_control_filter(
            control_channel=self.control_channel,
            command_processor=self.command_processor,
            output_adapter=self.output_adapter,
            session_checker=self._is_session_active,
            turn_uuid_getter=self._get_active_turn_uuid,
        )

        await self.input_adapter.start()

        self._router_task = asyncio.create_task(self.workspace_stack.dispatcher.run())
        print(f"[OK] WorkspaceDispatcher running, {len(self._pools)} pools active")
        await self._shutdown_event.wait()

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
        if hasattr(self, "_router_task") and self._router_task:
            self._router_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._router_task
        # Stop EVERY materialized workspace's resources (background + pools +
        # broker + terminals) — not just home, so multi-live workspaces don't
        # leak background tasks/brokers on shutdown.
        if (
            self.workspace_stack is not None
            and not await self.workspace_stack.registry.evict_all()
        ):
            raise BotServiceShutdownIncompleteError(
                "workspace shutdown incomplete; shared resources retained for retry"
            )
        # Shut down the shared MCP registry AFTER evicting workspaces: evict_all
        # calls _stop_resources → McpBackend.release() per pool, which on the
        # shared path only DETACHES the facade (real connections are shared and
        # service-scoped). The registry then closes the actual subprocesses.
        # Order matters: release facades first, then close the real connections.
        if self._mcp_registry is not None:
            with contextlib.suppress(BaseException):
                await self._mcp_registry.shutdown()
        with contextlib.suppress(BaseException):
            await self.input_adapter.stop()
        if self._pool_session_store is not None:
            with contextlib.suppress(BaseException):
                self._pool_session_store.close()
            self._pool_session_store = None
        if self._home_persistence is not None:
            with contextlib.suppress(BaseException):
                await self._home_persistence.close()
            self._home_persistence = None
        # T26: registry DB closes LAST — after all workspaces are evicted (their
        # workspace DBs close inside _stop_resources) and after the MCP registry
        # and input adapter stop. The registry DB is the global, last-to-close
        # persistence layer.
        if self._registry_persistence is not None:
            with contextlib.suppress(BaseException):
                await self._registry_persistence.close()
            self._registry_persistence = None
