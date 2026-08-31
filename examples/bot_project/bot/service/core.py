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
    from bot.service.media_store import WorkspaceScopedMediaStore
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.persistence.managers import (
        RegistryPersistenceManager,
        WorkspacePersistenceManager,
    )
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

from bot.service._model_config_loader import _apply_bot_model_config, _load_app_config
from bot.service.errors import BotServiceShutdownIncompleteError
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.model_config import BotModelConfig
from bot.service.model_provider import BotModelProvider
from bot.service.pool.declaration import (
    apply_workspace_resource_selection,
    load_scope_declaration_opt,
    validate_workspace_mcp_set,
    workspace_layer_present,
    workspace_mcp_prewarm_names,
)
from bot.utils.config_loader import ConfigLoader
from bot.workspace.wiring import build_workspace_stack
from modex_agent import (
    LLMProvider,
)
from modex_agent.agents.external.providers.opencode.server_manager import (
    OpenCodeServerManager,
)
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.llm_struct import (
    DeadlinePolicy,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.core.session_store import SessionStore
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_instance import PoolInstance
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter
from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR, WORKSPACE_STATE_DB

from .builders import (
    AgentBuilderMixin,
    _build_control_channel,
    _build_main_command_processor,
)

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
        emitter_factory: Callable[[str, str], ContentEmitter[Any]],
        *,
        app_config: AppConfig | None = None,
        # ── Injection points for pool creation ──
        output_adapter_factory: Callable[[], OutputAdapter] | None = None,
        on_subagent_created: Callable[[str, str, str], Awaitable[None]] | None = None,
        session_registry: SessionRegistry | None = None,
        session_store: SessionStore | None = None,
        media_store: WorkspaceScopedMediaStore | None = None,
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
            self._bot_model_config = _apply_bot_model_config(self.config_dir, app_config)
        self._output_adapter_factory = output_adapter_factory
        self._on_subagent_created = on_subagent_created

        # SessionInfo registry/store — injected into every pool so subagent
        # sessions are registered with their parent_session_id and resolvable
        # at dispatch time (SubagentAutoSendHook needs parent to notify).
        self._session_registry: SessionRegistry | None = session_registry
        self._session_store: SessionStore | None = session_store
        self._media_store = media_store

        # Multi-live workspace stack (built in initialize). Owns the registry,
        # conversation map, resolver, controller, dispatcher, factory. The
        # controller is the per-conversation WorkspaceControlPort passed to
        # the cd/exit/pwd handlers; ``workspace_context`` is a compat alias.
        self.workspace_stack: Any = None
        self.workspace_context: Any = None
        # The loaded scope declaration (ticket 14): ``None`` when
        # config/scopes/bot.yml is absent. Its workspace layer selects the
        # multi-live stack shape (N15 — the ``workspace.enabled`` flag is
        # dead; declaration absence IS the single-workspace form) and its
        # resource-selection overrides resolve onto ``_app_config`` at boot.
        self._scope_spec: Any = None
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

        # Shared MCP connection registry (ADR-0017 Task 5a). Service-scoped,
        # concurrent, dedup-by-config-hash. Built in initialize() when the
        # ``sharedRegistry`` flag (config/mcp/registry.json, default ON) is set;
        # pools then acquire SharedMcpBackend facades via registry.acquire.
        # None when the flag is off (or registry absent) → legacy per-pool path.
        self._mcp_registry: McpConnectionRegistry | None = None

        # Execution-strategy registry (ADR-0025, ticket 3). Built in
        # initialize() with the shipped strategies (react in ticket 3;
        # external added in ticket 4). Threaded through wiring.py into
        # create_pool so react pools are assembled via
        # ReactExecutionStrategy.assemble() instead of inline _build_* calls.
        self._strategy_registry: Any = None

        # Component registry (loaded in initialize() before pool creation).
        # The registry holds all plugin-registered component factories
        # (DefaultPlugin bundled + project plugins from
        # examples/bot_project/plugins/); pool/agent config lives in the
        # scope declaration (config/scopes/bot.yml).
        self._component_registry: Any = None
        self._service_assembly_ctx: AssemblyContext | None = None

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
        logger.info("=" * 60)
        logger.info(">> Initializing Bot Service")
        logger.info("=" * 60)

        # ADR-0027 T8: register cooperative SIGTERM/SIGINT handlers that
        # run atexit cleanup (killing any live ``opencode serve``
        # subprocesses) before exit. Idempotent — safe to call every boot.
        from modex_agent.agents.external.os_layer import (
            register_signal_handlers,
        )

        register_signal_handlers()

        # 1. Load config (IOC AppConfig is the only source of truth)
        if self._app_config is None:
            self._app_config = _load_app_config(self.config_dir)
            self._bot_model_config = _apply_bot_model_config(self.config_dir, self._app_config)
        assert self._app_config is not None, "AppConfig must be loaded before initialize"

        # Ticket 14 (SPEC §3.1): the scope declaration's workspace layer is
        # the resource-selection authority. Loading it here decides (a) the
        # stack shape — a workspace-layer declaration boots the multi-live
        # stack; its absence (pool-as-root or no declaration) boots the
        # single-workspace deployment (N15), and (b) the resource-selection
        # overrides (memory backend, path layout) resolved onto the config
        # view every workspace-scoped consumer reads. Malformed
        # declarations fail the boot loudly.
        self._scope_spec = load_scope_declaration_opt(
            self._project_dir / "config" / "scopes" / "bot.yml"
        )
        self._app_config = apply_workspace_resource_selection(
            self._app_config, self._scope_spec
        )
        declared_pool_count = (
            len(self._scope_spec.workspace.pools)
            if self._scope_spec is not None and self._scope_spec.workspace is not None
            else (1 if self._scope_spec is not None and self._scope_spec.pool is not None else 0)
        )
        logger.info("Config loaded (%d declared pools)", declared_pool_count)

        # Shared MCP connection registry (ADR-0017 Task 5a). Service-scoped,
        # concurrent, dedup-by-config-hash. When the sharedRegistry flag is on
        # (default), all pools/agents/workspaces share one subprocess per
        # configured server and switching/first-open of workspaces is free.
        # start_connecting fires all supervisors now so connections are READY
        # by the time the home workspace's pools build (which call
        # the Stage-4 MCP loader → registry.acquire and find them instantly).
        # Set ``sharedRegistry: false`` in config/mcp/registry.json to disable
        # and fall back to today's per-pool MCPClientManager path.
        from bot.config.mcp_registry import read_registry, read_shared_registry_flag
        from modex_agent.ioc.configs.app import _resolve_env_in
        from modex_agent.tools.mcp.injector import JsonFileMCPTransportInjector
        from modex_agent.tools.mcp.registry import McpConnectionRegistry

        mcp_registry_path = self._project_dir / "config" / "mcp" / "registry.json"
        if read_shared_registry_flag(mcp_registry_path):
            raw_servers = read_registry(mcp_registry_path)
            # Ticket 14: the declared workspace MCP set is validated loudly
            # against the registry (typo'd names abort the boot) and scopes
            # the pre-warm; undeclared workspaces pre-warm everything.
            validate_workspace_mcp_set(self._scope_spec, raw_servers)
            prewarm_names = workspace_mcp_prewarm_names(self._scope_spec, raw_servers)
            if raw_servers:
                # ${ENV} interpolation MUST happen before the registry hashes
                # and connects, else tokens like ${MY_TOKEN} reach the
                # subprocess literally.
                servers = _resolve_env_in(raw_servers)
                self._mcp_registry = McpConnectionRegistry(
                    servers=servers,
                    injector=JsonFileMCPTransportInjector(),
                )
                self._mcp_registry.start_connecting(prewarm_names)
                logger.info("Shared MCP registry: %d server(s) connecting concurrently", len(servers))
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
            # Ticket 14 (N15): the declaration form selects the stack — a
            # workspace layer boots multi-live; its absence (pool-as-root /
            # no declaration) boots the single-workspace deployment. The
            # ``workspace.enabled`` config flag is dead.
            self._model_choice_registry = ModelChoiceRegistry()
            self._default_provider = self._build_default_provider()
            self.control_channel = _build_control_channel(self.control_channel)
            self.command_processor = _build_main_command_processor()

            # Component registry: load DefaultPlugin (bundled FW defaults) +
            # project plugins from examples/bot_project/plugins/ (BotStrategies,
            # BotHooks, IMInputStages, and any user-added plugins). Loaded once
            # at service level so all pools share the same factory set.
            from modex_agent.plugins.defaults import DefaultPlugin
            from modex_agent.plugins.loader import (
                ComponentRegistryLoader,
                PluginDiscoveryConfig,
            )
            from modex_agent.plugins.registry import (
                ComponentRegistry,
                strategy_registry_from_components,
            )

            self._component_registry = ComponentRegistry()
            await ComponentRegistryLoader.load(
                self._component_registry,
                PluginDiscoveryConfig(
                    bundled_factories=(DefaultPlugin(),),
                    project_plugin_paths=(self._project_dir / "plugins",),
                ),
            )
            self._strategy_registry = strategy_registry_from_components(
                self._component_registry
            )
            logger.info("Component registry: %s", self._project_dir / "plugins")

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
                    self._project_dir / self._app_config.paths.data_dir_name / WORKSPACE_STATE_DB
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

            self.workspace_stack = build_workspace_stack(
                self,
                data_dir_name=self._app_config.paths.data_dir_name,
                enabled=workspace_layer_present(self._scope_spec),
            )
            self.workspace_context = self.workspace_stack.controller
            from modex_agent.plugins.assembly.context import AssemblyContext

            self._service_assembly_ctx = AssemblyContext(
                registry=self._component_registry,
                workspace_ctx=self.workspace_stack.registry.home_context,
                workspace_registry=self.workspace_stack.registry,
            )
            await self.workspace_stack.registry.initialize()

            # Ticket 17: runtime-created workspaces persist as declaration
            # files under config/scopes/workspaces/. Re-register each at
            # boot (lazily materialized on the first turn that targets
            # them — the same road a /cd-switched workspace takes).
            from bot.workspace.dynamic_workspaces import register_dynamic_workspaces

            await register_dynamic_workspaces(self)

            # Eagerly materialize the HOME workspace so its pools/router are live
            # for BotService.start/stop (v1 = home-only materialization). The
            # dispatcher lazily materializes other workspaces on first turn.
            self._home_resources = await self.workspace_stack.registry.materialize(
                self.workspace_stack.registry.home_context
            )
            self._pools = self._home_resources.pools
            self.pool_router = self._home_resources.pool_router
            self._print_pool_info()

            logger.info("=" * 60)
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
        logger.info("Pools: %s", list(self._pools.keys()))
        for name, pi in self._pools.items():
            logger.info("  %s: %s + %d subagents", name, pi.root_agent_name, pi.subagent_count)
        logger.info("Switch commands: /%s", " /".join(self._pools.keys()))
        logger.info("Default pool: %s", self._default_pool_name)

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # System-prompt resolution (per-pool, cached)
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Workspace helpers
    # ------------------------------------------------------------------ #

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
                    hook_timeout_seconds=s.turn.hook_timeout,
                    tool_timeout_seconds=s.turn.tool_timeout,
                ),
                deadline=DeadlinePolicy(
                    chunk_renew_seconds=s.deadline.chunk_renew_seconds,
                    max_ahead_seconds=s.deadline.max_ahead_seconds,
                    watchdog_poll_seconds=s.deadline.watchdog_poll_seconds,
                ),
            )
        else:
            from modex_agent.core.constants import DefaultValues

            policy = RuntimeSafetyPolicy(
                llm=LLMTimeoutPolicy(
                    request_timeout_seconds=None,
                    stream_idle_timeout_seconds=None,
                    framework_max_retries=2,
                    retry_backoff_seconds=(2.0, 8.0),
                ),
                turn=TurnTimeoutPolicy(
                    hook_timeout_seconds=10.0,
                    tool_timeout_seconds=DefaultValues.TOOL_TIMEOUT_SECONDS,
                ),
            )
        self._safety_policy_cache = policy
        return policy

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

        # The shared ``opencode serve`` singleton is bound to this bot's
        # lifetime — ``async with`` is the sole shutdown trigger.
        async with OpenCodeServerManager.lifecycle():
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
            logger.info("WorkspaceDispatcher running, %d pools active", len(self._pools))
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
        if self.workspace_stack is not None and not await self.workspace_stack.registry.evict_all():
            raise BotServiceShutdownIncompleteError(
                "workspace shutdown incomplete; shared resources retained for retry"
            )
        # Close cached real providers (each HTTPStreamProvider owns an httpx
        # client) AFTER evicting workspaces — order matters: pools and
        # background tasks stop inside evict_all and may still need them.
        # getattr guard: partial-init instances (tests build via __new__) skip.
        if isinstance(getattr(self, "_default_provider", None), BotModelProvider):
            with contextlib.suppress(BaseException):
                await self._default_provider.aclose()
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
