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

if TYPE_CHECKING:
    from modex_agent.commands.processor import SlashCommandProcessor
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry

from bot.plugins.integration import PluginIntegration
from bot.service.pool_router import PoolSessionStore
from bot.workspace.wiring import build_single_workspace_stack, build_workspace_stack
from bot.utils.config_loader import ConfigLoader
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
from modex_agent.ioc.configs.agent import AgentConfig as IOCAgentConfig
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.ioc.configs.memory import MemoryConfig as IOCMemoryConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter

from .builders import AgentBuilderMixin, resolve_system_prompt
from .pool_instance import PoolInstance

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
        self._pool_session_store: PoolSessionStore | None = None
        self.plugin_integration: PluginIntegration | None = None


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
    def _default_pool_name(self) -> str:
        """Default pool name from config."""
        if self._app_config is not None:
            return self._app_config.multi_agent.default_pool
        return "main"

    def _is_webui(self) -> bool:
        """Whether this service runs the WebUI (class attribute ``webui``)."""
        return self.webui

    def _build_default_provider(self) -> LLMProvider:
        """Build the default pool's LLM provider.

        The Workspace uses a single provider for its memory (summarizer) layer
        across all pools — pools may declare different LLMs for chat, but the
        memory system's summarizer is pool-independent, so the default pool's
        provider is sufficient.
        """
        assert self._app_config is not None, "AppConfig not loaded"
        pool_cfg = self._app_config.pools[self._default_pool_name]
        return create_llm_provider(pool_cfg.llm)

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
        return AppConfig.from_yaml(self.config_dir / "bot_config.yml")

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

        # Service-level session→pool mapping store. Lives in the project home
        # data dir so every workspace's PoolRouter and the WebUI pipeline share
        # one durable mapping. Without this, a mapping written by the WebUI in
        # the home workspace is invisible to a non-home workspace's PoolRouter.
        self._pool_session_store = PoolSessionStore(
            data_dir=self.config_dir.parent / self._app_config.paths.data_dir_name
        )

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

        # 1.5 Build the default LLM provider + the workspace stack.
        # Branch on workspace.enabled: False -> single-home stack (no /cd);
        # True -> full multi-live stack.
        self._default_provider = self._build_default_provider()
        self.control_channel = self._build_control_channel()
        self.command_processor = self._build_main_command_processor()
        self.plugin_integration = PluginIntegration(config={"enabled": False})
        if self._app_config.workspace.enabled:
            self.workspace_stack = build_workspace_stack(
                self, data_dir_name=self._app_config.paths.data_dir_name
            )
        else:
            self.workspace_stack = build_single_workspace_stack(
                self, data_dir_name=self._app_config.paths.data_dir_name
            )
        self.workspace_context = self.workspace_stack.controller

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

    def _print_pool_info(self) -> None:
        """Display pool configuration summary."""
        print(f"\n[INFO] Pools: {list(self._pools.keys())}")
        for name, pi in self._pools.items():
            subagent_count = sum(1 for a in pi.config.agents if a.role == "subagent")
            print(f"   {name}: {pi.main_agent_name} + {subagent_count} subagents")
        print(f"[INFO] Switch commands: /{' /'.join(self._pools.keys())}")
        print(f"[INFO] Default pool: {self._app_config.multi_agent.default_pool}")

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # System-prompt resolution (per-pool, cached)
    # ------------------------------------------------------------------ #

    def _system_prompt_for(self, name: str) -> str:
        """Resolve the pool's main-agent system prompt (cached per pool)."""
        cached = self._system_prompt_cache.get(name)
        if cached is not None:
            return cached
        assert self._app_config is not None
        pool_cfg = self._app_config.pools[name]
        main_cfg = next(
            (a for a in pool_cfg.agents if a.role == "main"),
            pool_cfg.agents[0],
        )
        prompt = resolve_system_prompt(main_cfg, self._project_dir)
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

    def _find_subagent_cfg(self) -> IOCAgentConfig | None:
        """Find the first subagent config by role."""
        if not self._app_config or not self._app_config.agents:
            return None
        for a in self._app_config.agents:
            if a.role == "subagent":
                return a

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
                    tool_timeout_seconds=360.0,
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
        # Start the input adapter, each HOME pool's broker bridge, then the
        # workspace dispatcher (resolves the conversation's workspace per
        # message and routes into that workspace's pool_router).
        # Dream + curator background tasks are workspace-scoped and were
        # started inside build_resources when each workspace materialized.

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
        for pool in self._home_resources.pools.values():
            await pool.broker_bridge.start()

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
        if self.workspace_stack is not None:
            with contextlib.suppress(BaseException):
                await self.workspace_stack.registry.evict_all()
        with contextlib.suppress(BaseException):
            await self.input_adapter.stop()
