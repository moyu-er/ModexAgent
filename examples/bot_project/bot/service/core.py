"""BotService core — generic bot orchestration for any InputAdapter/OutputAdapter pair.

Supports two runtime modes:
- pipeline: single AgentPipeline, SubagentManager creates asyncio.Task directly.
- pool: AgentPool with resident agents, BrokerBridgeService routes messages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from framework.commands.processor import SlashCommandProcessor

from bot.plugins.integration import PluginIntegration
from bot.utils.config_loader import ConfigLoader
from framework import (
    AgentPipeline,
    InMemoryToolManager,
    ReActAgent,
    ToolManagerConfig,
)
from framework.control.channel import InMemoryControlChannel
from framework.control.ui.im import IMUserInterface
from framework.core.emitter import ContentEmitter
from framework.core.llm_struct import (
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from framework.core.skills import (
    DirectorySkillCache,
    FileSkillSource,
    ProgressiveBuilder,
    ResolutionContext,
    SkillManager,
)
from framework.hook.builtin import InboxFlushHook
from framework.interceptor.builtin import (
    ControlDrainInterceptor,
    ToolResultLimitInterceptor,
)
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
from framework.interceptor.chain import InterceptorChain
from framework.ioc.configs.agent import AgentConfig as IOCAgentConfig
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.memory import MemoryConfig as IOCMemoryConfig
from framework.ioc.factories.governance import create_governance, create_peer_governance
from framework.ioc.factories.llm import create_llm_provider
from framework.ioc.factories.memory import create_memory
from framework.memory.core.scope import MemoryContext
from framework.memory.injection import FullInjectionPolicy
from framework.memory.system import MemorySystemContextManager
from framework.messaging.broker_bridge import (
    BrokerBridgeService,
    OutputRoute,
)
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentAddress,
    AgentDescriptor,
    AgentFactory,
    AgentPool,
    DefaultAgentFactory,
    SubagentManager,
    TaskCoordinationConfig,
)
from framework.multi_agent.descriptor import AgentLLMConfig
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.pipeline.adapters import InputAdapter, OutputAdapter
from framework.tools.overflow.cleaner import OverflowCleaner

from .builders import AgentBuilderMixin

logger = logging.getLogger(__name__)


class BotService(AgentBuilderMixin):
    """Generic bot service supporting arbitrary InputAdapter/OutputAdapter pairs.

    Can be used for QQ, Discord, Feishu, DingTalk, Telegram, CLI, etc.
    Just provide the corresponding adapters and an Emitter factory.

    Modes:
    - pipeline: single AgentPipeline (default). SubagentManager spawns asyncio.Task.
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

        # Components
        self.pipeline: AgentPipeline | None = None
        self.agent_pool: AgentPool | None = None
        self.broker_bridge: BrokerBridgeService | None = None
        self.agent_bus: Any | None = None
        self.tool_manager: InMemoryToolManager | None = None
        self.mcp_manager: Any | None = None
        self.memory_system: Any | None = None
        self.context_manager: Any | None = None
        self.auto_compact_service: Any | None = None
        self.agent: ReActAgent | None = None
        self.agent_factory: AgentFactory | None = None
        self.subagent_manager: SubagentManager | None = None
        self.broker: InMemoryMessageBroker | None = None
        self.inbox_server: LocalFileInboxServer | None = None
        self.inbox_producer: InboxProducer | None = None
        self.inbox_consumer: InboxConsumer | None = None

        # Runtime components
        self.provider: Any | None = None
        self.plugin_integration: Any | None = None
        self.dream_engine: Any | None = None

        # Subagent/peer caches
        self._subagent_skill_managers: dict[str, SkillManager] = {}
        self._subagent_memory_systems: dict[str, Any] = {}
        self._peer_memory_systems: dict[str, Any] = {}

        # Auto-compact
        self._auto_compact_task: asyncio.Task | None = None

        # Overflow cleaner
        self._overflow_cleaner: OverflowCleaner | None = None

        # Control plane
        self.control_channel: InMemoryControlChannel | None = None
        self.interceptor_chain: InterceptorChain | None = None
        self._safety_policy_cache: RuntimeSafetyPolicy | None = None

        # Approval
        self._approval_workspace: Path | None = None
        self._im_ui: IMUserInterface | None = None
        self._turn_store: Any | None = None
        self._command_store: Any | None = None

        # Runtime control
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    @property
    def _main_agent_cfg(self) -> IOCAgentConfig | None:
        """Find main agent by role, not by index."""
        if not self._app_config or not self._app_config.agents:
            return None
        for a in self._app_config.agents:
            if a.role == "main":
                return a
        # Fallback: first agent if no role="main" found
        return self._app_config.agents[0]

    @property
    def _main_memory_cfg(self) -> IOCMemoryConfig | None:
        """Memory config for the first agent."""
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

    @property
    def _project_dir(self) -> Path:
        """Project root directory (where bot_service.py lives)."""
        return Path(__file__).parent.parent.parent

    def _resolve_path(self, config_key: str, default_relative: str) -> Path:
        """Resolve a path from AppConfig paths, falling back to a relative default."""
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
        print(f"[OK] Config loaded ({len(self._app_config.agents)} agents via IOC)")

        # 2. Create Broker
        self.broker = InMemoryMessageBroker()
        await self.broker.start()
        print("[OK] Broker initialized")

        # 3. Create ToolManager
        tm_config = ToolManagerConfig(
            max_workers=10,
            enable_parallel=True,
            parallel_max_workers=5,
        )
        self.tool_manager = InMemoryToolManager(config=tm_config)

        await self._register_tools()
        await self._register_mcp_tools()
        print(f"[OK] ToolManager initialized, {len(self.tool_manager.list_tools())} tools registered")

        # -- Plugin system --
        # Plugins are OFF by default. Only enabled if `plugins:` section
        # in bot_config.yml has `enabled: true`.
        plugins_cfg = self._app_config.plugins
        if plugins_cfg is not None and plugins_cfg.enabled:
            local_plugins_dir = Path(__file__).parent.parent.parent / "plugins"
            self.plugin_integration = PluginIntegration(
                plugins_cfg.model_dump(),
                extra_plugin_dirs=[local_plugins_dir] if local_plugins_dir.exists() else [],
            )
            has_plugins = await self.plugin_integration.discover_and_load()
            if has_plugins:
                self.plugin_integration.inject_tools(self.tool_manager)
                print(f"[OK] Plugin system loaded, {len(self.plugin_integration.list_plugins())} plugins active")
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
        data_dir = self._resolve_path("data_dir", "data")
        memory_dir = self._resolve_path("memory_dir", str(Path(data_dir) / "memory"))
        memory_dir.mkdir(parents=True, exist_ok=True)
        main_cfg = self._main_agent_cfg
        main_memory_cfg = main_cfg.memory if main_cfg else None
        if main_memory_cfg is not None:
            self.memory_system = create_memory(main_memory_cfg, self.provider, memory_dir)
            await self.memory_system.initialize()
        else:
            self.memory_system = None

        self.context_manager = MemorySystemContextManager(
            memory_system=self.memory_system,
            default_agent_id=main_cfg.name if main_cfg else "main",
            default_agent_role="main",
            base_system_prompt=main_cfg.system_prompt if main_cfg else "",
            injection_policy=FullInjectionPolicy(),
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

        # 5.5 Initialize long-term defaults, auto-compact, and dream engine
        await self._init_long_term_defaults(data_dir, main_memory_cfg)
        await self._init_auto_compact(main_memory_cfg)
        self._init_dream()

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
            builder = ProgressiveBuilder(base_path=self._project_dir)
            main_skill_manager = SkillManager(
                source=source, builder=builder, cache=cache,
            )
            available_skills = await main_skill_manager.list_skills(
                ResolutionContext.from_runtime(tool_manager=self.tool_manager)
            )
            print(f"[OK] SkillManager initialized, main agent loaded {len(available_skills)} skills")

            self.plugin_integration.inject_skill_sources(main_skill_manager)
            print("[OK] Plugin skill sources injected")
        else:
            print(f"[WARN] Skills directory not found: {main_skills_dir}")

        # 6.5 Create InboxServer / Producer / Consumer
        inbox_dir = self._resolve_path("inbox_dir", "data/inbox")
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
            from framework.interceptor.builtin.result_limit import ToolResultLimitInterceptor
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
        self._approval_workspace = self._project_dir / "data/approval"
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

        runtime_data_dir = self._project_dir / "data" / "runtime_state"
        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        self._turn_store = JsonFileTurnStateStore(runtime_data_dir / "turns", codec_registry)
        self._command_store = JsonFileRuntimeCommandStore(runtime_data_dir / "commands")
        print("[OK] Typed runtime stores initialized (data/runtime_state/)")

        # 8. Create ReActAgent (main agent in full mode with approval)
        self.agent = ReActAgent(provider=self.provider, mode="full")
        print("[OK] ReActAgent initialized")

        # 9. Initialize runtime by mode
        if self.mode == "pipeline":
            await self._initialize_pipeline(main_skill_manager)
        elif self.mode == "pool":
            await self._initialize_pool(main_skill_manager)
        else:
            raise ValueError(f"Unsupported mode: {self.mode}")

        # 10. Register multi-agent tools (must happen after subagent_manager creation)
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
        print(f"   - SubagentManager: {type(self.subagent_manager).__name__}")
        print("   - InboxServer: LocalFileInboxServer")
        print(f"   - Mode: {self.mode}")

        print("=" * 60)

    async def _initialize_pipeline(self, main_skill_manager: SkillManager | None) -> None:
        """Initialize pipeline-mode runtime."""
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
        inbox_flush_hook = InboxFlushHook(
            consumer=self.inbox_consumer,
            agent_name=parent_agent_name,
        )

        self.subagent_manager = SubagentManager(
            broker=self.broker,
            agent_factory=self.agent_factory,
            coordination_config=TaskCoordinationConfig(
                enable_for_subagent=True,
                default_timeout_seconds=180.0,
            ),
            on_task_complete=self._cleanup_subagent_memory,
        )
        print("[OK] SubagentManager initialized")

        if self.agent is None:
            raise RuntimeError("Agent is not initialized")
        if self.tool_manager is None:
            raise RuntimeError("ToolManager is not initialized")

        pipeline_hooks = [inbox_flush_hook]
        pipeline_hooks.extend(self._collect_run_hooks())

        # Build AgentRuntime via framework RuntimeAssembler
        runtime = await self._assemble_runtime(hooks=self._build_hook_runner(pipeline_hooks))
        command_processor = self._build_main_command_processor(main_skill_manager)

        self.pipeline = AgentPipeline(
            agent=self.agent,
            context_manager=self.context_manager,
            tool_manager=self.tool_manager,
            input_adapter=self.input_adapter,
            output_adapter=self.output_adapter,
            emitter_factory=self.emitter_factory,
            dream_engine=self.dream_engine,
            dream_interval=300,
            max_iterations=main_cfg.max_steps if main_cfg else 40,
            skill_manager=main_skill_manager,  # type: ignore[arg-type]
            hooks=pipeline_hooks,
            hook_runner=self._build_hook_runner(pipeline_hooks),
            interceptor_chain=self.interceptor_chain,
            subagent_manager=self.subagent_manager,
            context_manager_factory=self._get_context_manager,
            governance=create_governance(self._main_memory_cfg, self._app_config.llm.max_tokens),
            safety=self.safety_policy,
            approval_workspace=str(self._approval_workspace),
            user_interface=self._im_ui,
            turn_store=self._turn_store,
            command_store=self._command_store,
            runtime_services=runtime.services,
            command_processor=command_processor,
        )
        print("[OK] AgentPipeline initialized")
        print(f"   Input: {self.input_adapter.name}")
        print(f"   Output: {self.output_adapter.name}")

    async def _initialize_pool(self, _main_skill_manager: SkillManager | None) -> None:
        """Initialize pool-mode runtime."""
        from framework.ioc.factories.descriptors import (
            build_subagent_descriptor,
        )
        from framework.multi_agent.bus import LocalAgentMessageBus

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

        # Create AgentMessageBus
        self.agent_bus = LocalAgentMessageBus(
            producer=self.inbox_producer,
            consumer=self.inbox_consumer,
            broker=self.broker,
        )
        print("[OK] LocalAgentMessageBus initialized")

        # SubagentManager
        self.subagent_manager = SubagentManager(
            broker=self.broker,
            agent_factory=self.agent_factory,
            coordination_config=TaskCoordinationConfig(
                enable_for_subagent=True,
                default_timeout_seconds=180.0,
            ),
            on_task_complete=self._cleanup_subagent_memory,
        )
        print("[OK] SubagentManager initialized")

        # Create AgentPool
        from framework.multi_agent.session_id import DefaultSessionIdStrategy

        self.agent_pool = AgentPool(
            broker=self.broker,
            agent_factory=self.agent_factory,
            default_context_manager=self.context_manager,
            agent_bus=self.agent_bus,
            inbox_consumer=self.inbox_consumer,
            enable_inbox_polling=True,
            inbox_poll_interval=10.0,
            default_context_manager_factory=self._get_context_manager,
            session_strategy=DefaultSessionIdStrategy(main_agent_name=parent_agent_name),
            safety=self.safety_policy,
        )

        # Register main agent as resident
        main_descriptor = AgentDescriptor(
            address=main_address,
            llm_config=AgentLLMConfig(
                model=self._app_config.llm.model,
                temperature=self._app_config.llm.temperature,
                max_tokens=self._app_config.llm.max_tokens,
            ),
            system_prompt_template=main_cfg.system_prompt if main_cfg else "",
            context_strategy="persistent",
            max_iterations=main_cfg.max_steps if main_cfg else 20,
            execution_strategy="react",
            safety_policy=self.safety_policy,
        )
        await self.agent_pool.register_resident(main_descriptor)
        print(f"[OK] AgentPool initialized, main agent '{parent_agent_name}' registered as resident")

        # Wire AgentRuntime services into main agent's pool pipeline
        main_instance = self.agent_pool._agents.get(parent_agent_name)
        if main_instance is not None and main_instance.pipeline is not None:
            # Build AgentRuntime via framework RuntimeAssembler
            runtime = await self._assemble_runtime(hooks=main_instance.pipeline.hook_runner)

            main_instance.pipeline.interceptor_chain = runtime.interceptors
            main_instance.pipeline.runtime_services = runtime.services
            main_instance.pipeline.turn_store = self._turn_store
            main_instance.pipeline._approval_workspace = self._approval_workspace
            main_instance.pipeline._user_interface = self._im_ui
            main_instance.pipeline.command_processor = self._build_main_command_processor(
                main_instance.pipeline.skill_manager
            )
            print("[OK] Main agent pool pipeline wired with AgentRuntime services")

        # Register subagents as residents (pool mode requires all targets to be resident)
        _memory_dir = self._resolve_path("memory_dir", "data/memory")
        subagent_cfg = self._find_subagent_cfg()
        if subagent_cfg is not None:
            descriptor, sub_tool_manager, sub_skill_manager, _memory_ctx = await build_subagent_descriptor(
                subagent_cfg, self._app_config, self._project_dir,
                _memory_dir, self.safety_policy, self.provider,
            )
            if descriptor.address.name != parent_agent_name:
                await self.agent_pool.register_resident(
                    descriptor,
                    tool_manager=sub_tool_manager,
                    skill_manager=sub_skill_manager,
                )
                # Inject lightweight governance into subagent pipeline
                sub_instance = self.agent_pool.get(descriptor.address.name)
                if sub_instance and sub_instance.pipeline:
                    sub_instance.pipeline.governance = create_peer_governance(
                        subagent_cfg.memory, self._app_config.llm.max_tokens,
                    )
                print(f"[OK] Subagent '{descriptor.address.name}' registered as resident")

        # Initialize peer agents
        await self._initialize_peer_agents()

        # Configure BrokerBridgeService
        self.broker_bridge = BrokerBridgeService(
            broker=self.broker,
            input_bindings={self.input_adapter: main_address},
            output_routes=[
                OutputRoute(
                    adapter=self.output_adapter,
                    match_topic=f"agent:{parent_agent_name}:out",
                ),
            ],
        )
        print("[OK] BrokerBridgeService initialized")
        print(f"   Input bridge: {self.input_adapter.name} -> {main_address}")
        print(f"   Output route: agent:{parent_agent_name}:out -> {self.output_adapter.name}")

    def _find_subagent_cfg(self) -> IOCAgentConfig | None:
        """Find the first subagent config by role."""
        if not self._app_config or not self._app_config.agents:
            return None
        for a in self._app_config.agents:
            if a.role == "subagent":
                return a
        return None

    def _find_peer_cfgs(self) -> list[IOCAgentConfig]:
        """Find all peer configs by role."""
        if not self._app_config or not self._app_config.agents:
            return []
        return [a for a in self._app_config.agents if a.role == "peer"]

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
                    agent_run_timeout_seconds=180.0,
                    hook_timeout_seconds=10.0,
                    tool_timeout_seconds=60.0,
                ),
            )
        self._safety_policy_cache = policy
        return policy

    def _collect_run_hooks(self) -> list[Any]:
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

    def _build_hook_runner(self, hooks: list[Any]) -> Any:
        """Build HookRunner from collected hooks with default HookSpec.

        Explicitly injects RuntimeContextHook so PeerAutoSendHook can detect
        communication tool calls. Previously this was auto-injected by
        AgentPipeline into its hooks list, but ReActAgent prefers hook_runner
        and never falls back to hooks — causing the hook to be silently ignored.
        """
        from framework.hook import HookErrorPolicy, HookRunner, HookSpec
        from framework.hook.builtin import RuntimeContextHook

        runner = HookRunner()
        # RuntimeContextHook must be in hook_runner (not just hooks list)
        # so that ReActAgent._call_hooks() actually dispatches it.
        runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))
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
          1. ControlDrainInterceptor  – consumes cancel/timeout commands
          2. ToolResultLimitInterceptor – truncates long tool results
        """
        if self.interceptor_chain is not None:
            return self.interceptor_chain

        channel = self._build_control_channel()
        chain = InterceptorChain()

        # 1. Control drain – highest priority, processes cancel commands
        chain.add(ControlDrainInterceptor(channel=channel, max_commands=3))

        # 2. Tool result overflow
        from framework.tools.overflow.cleaner import OverflowCleaner
        from framework.tools.overflow.handler import ToolResultOverflowHandler
        from framework.tools.overflow.local import LocalFileToolOverflowStore

        overflow_dir = self._project_dir / "data"
        max_chars = 10000
        overflow_store = LocalFileToolOverflowStore(workspace=overflow_dir)
        overflow_cleaner = OverflowCleaner(overflow_store)
        overflow_handler = ToolResultOverflowHandler(
            store=overflow_store,
            cleaner=overflow_cleaner,
            max_chars=max_chars,
        )
        chain.add(ToolResultLimitInterceptor(
            overflow_handler=overflow_handler,
            max_chars=10000,
        ))

        self.interceptor_chain = chain
        return chain

    def _build_main_command_processor(
        self,
        skill_manager: SkillManager | None,
    ) -> SlashCommandProcessor:
        from framework.commands.processor import SlashCommandProcessor

        _ = skill_manager
        return SlashCommandProcessor.default()

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the service."""
        print("\n" + "=" * 80)
        print(">> Starting Bot Service")
        print("=" * 80)

        if self.mode == "pipeline":
            if self.pipeline is None:
                print("[WARN] Pipeline not initialized, cannot start")
                return
            pipeline_task = asyncio.create_task(self.pipeline.run())
            self._tasks.append(pipeline_task)
        elif self.mode == "pool":
            if self.agent_pool is None or self.broker_bridge is None:
                print("[WARN] AgentPool or BrokerBridge not initialized, cannot start")
                return
            await self.broker_bridge.start()
            print("[OK] BrokerBridgeService started")
            # Start DreamEngine background loop for pool mode
            if self.dream_engine is not None:
                de_cfg = self._main_memory_cfg.dream_engine if self._main_memory_cfg else None
                dream_interval = de_cfg.interval if de_cfg else 300
                dream_task = asyncio.create_task(self._dream_background_loop(dream_interval))
                self._tasks.append(dream_task)
                print(f"[OK] DreamEngine background loop started (interval={dream_interval}s)")
        else:
            print(f"[WARN] Unknown mode: {self.mode}")
            return

        print("\n" + "=" * 80)
        print(f"[OK] Bot Service (Multi-Agent, mode={self.mode}) started!")
        print(f"   Input: {self.input_adapter.name}")
        print(f"   Output: {self.output_adapter.name}")
        print(f"   Model: {self._app_config.llm.model}")
        print("   Log: logs/bot.log")
        print("=" * 80)
        print("   Waiting for messages...\n")

        def signal_handler(sig: int, _frame: Any) -> None:
            print(f"\n[STOP] Received signal {sig}, shutting down...")
            self._shutdown_event.set()

        signal.signal(signal.SIGINT, signal_handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, signal_handler)

        try:
            await self._shutdown_event.wait()
        except KeyboardInterrupt:
            print("\n[STOP] Shutting down...")
        finally:
            await self.stop()

    # ------------------------------------------------------------------ #
    # Runtime assembly
    # ------------------------------------------------------------------ #

    async def _assemble_runtime(self, hooks: Any = None) -> Any:
        """Build AgentRuntime via framework RuntimeAssembler.

        The only difference between pipeline and pool mode is the hooks source.
        Everything else — classifier, strategy, control, governance — is identical.
        """
        from framework.agents.react.approval import TieredToolApprovalClassifier
        from framework.agents.react.assembler import RuntimeAssembler, RuntimeServicesConfig
        from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
        from framework.control.store import InMemoryControlStore
        from framework.control.types import ControlCommandType
        from framework.interceptor.handler import DefaultCancelHandler

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

        runtime = await RuntimeAssembler.assemble(RuntimeServicesConfig(
            mode="full",
            hooks=hooks,
            interceptors=list(self.interceptor_chain.interceptors) if self.interceptor_chain else None,
            approval_classifier=TieredToolApprovalClassifier(
                config=approval_config,
                argument_matcher=ArgumentMatcher(project_root=self._project_dir),
            ),
            control_channel=self.control_channel,
            control_store=InMemoryControlStore(),
            command_handlers=[(ControlCommandType.CANCEL_TURN, DefaultCancelHandler())],
            turn_store=self._turn_store,
            project_root=self._project_dir,
            governance=create_governance(self._main_memory_cfg, self._app_config.llm.max_tokens),
            safety=self.safety_policy,
        ))
        print(f"[OK] AgentRuntime built (approval enabled={enabled}, tools={list(tools_approval.keys())})")
        return runtime

    # ------------------------------------------------------------------ #
    # Memory helpers
    # ------------------------------------------------------------------ #

    async def _init_long_term_defaults(
        self,
        _data_dir: Path,
        main_memory_cfg: IOCMemoryConfig | None,
    ) -> None:
        """Initialize default long-term memory files if enabled and not present."""
        if main_memory_cfg is None or main_memory_cfg.long_term is None:
            return
        if not main_memory_cfg.long_term.init_defaults:
            return
        if self.memory_system is None:
            return

        lt_mgr = self.memory_system.knowledge_manager
        if lt_mgr is None:
            return

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

        # Wire auto-consolidation into knowledge manager
        summarizer = getattr(self, "_summarizer_agent", None)
        if summarizer is not None and hasattr(lt_mgr, "_consolidation_fn"):
            from framework.agents.summarizer.agent import SummarizerAgent

            async def _consolidate(content: str, _file_name: str) -> str:
                return await summarizer.summarize(
                    content,
                    prompt=SummarizerAgent.PROMPT_KNOWLEDGE_CONSOLIDATION,
                    max_tokens=2000,
                )

            lt_mgr._consolidation_fn = _consolidate
            print("   [OK] Knowledge auto-consolidation wired")

        print("   [OK] Long-term memory defaults ensured")

    async def _init_auto_compact(
        self,
        main_memory_cfg: IOCMemoryConfig | None,
    ) -> None:
        """Initialize and start background auto-compact via DefaultMemoryMaintenancePolicy."""
        if self.memory_system is None:
            return

        compression_coordinator = self.memory_system.compression_coordinator
        if compression_coordinator is None:
            return

        from framework.memory.lifecycle import DefaultMemoryMaintenancePolicy

        self._maintenance_policy = DefaultMemoryMaintenancePolicy(
            idle_threshold_seconds=1800,
            keep_recent_messages=8,
            compression_coordinator=compression_coordinator,
        )

        scan_interval = 300
        self._auto_compact_task = asyncio.create_task(
            self._auto_compact_loop(scan_interval)
        )
        print(
            f"   [OK] AutoCompactService started "
            f"(idle_threshold=1800s, scan_interval={scan_interval}s)"
        )

    async def _auto_compact_loop(self, interval: float) -> None:
        """Background loop for auto-compaction using maintenance policy."""
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
                    logger.info("Auto-compacted scopes: %s", compacted)
            except Exception:
                logger.exception("AutoCompact scan loop error")

    def _init_dream(self) -> None:
        """Initialize DreamEngine for offline memory consolidation."""
        if self._main_memory_cfg is None or self._main_memory_cfg.dream_engine is None:
            return
        if not self._main_memory_cfg.dream_engine.enabled:
            return
        if self.memory_system is None or self.provider is None:
            return
        if self.memory_system.archive_manager is None or self.memory_system.knowledge_manager is None:
            return

        from framework.memory.consolidation.dream_engine import DreamEngine

        de_cfg = self._main_memory_cfg.dream_engine
        self.dream_engine = DreamEngine(
            llm_provider=self.provider,
            history_manager=self.memory_system.archive_manager,
            long_term_manager=self.memory_system.knowledge_manager,
            registry=self.memory_system.store_registry,
            max_batch_size=20,
            max_iterations=10,
            summarizer=getattr(self, "_summarizer_agent", None),
        )
        print("   [OK] DreamEngine initialized")

    async def _dream_background_loop(self, interval: int = 300) -> None:
        """Background loop for DreamEngine in pool mode."""
        if self.dream_engine is None:
            return
        print(f"[OK] DreamEngine background loop starting (interval={interval}s)")
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval)
                if self._shutdown_event.is_set():
                    break
                processed = await self.dream_engine.scan_all()
                if processed:
                    print(f"[Dream] Processed {len(processed)} scopes")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("DreamEngine background loop error")

    async def stop(self) -> None:
        """Stop the service."""
        print("\n[STOP] Shutting down service...")
        self._shutdown_event.set()

        if self.pipeline:
            try:
                print("   Stopping Pipeline...")
                await self.pipeline.stop()
                print("   [OK] Pipeline stopped")
            except Exception as e:
                print(f"   [WARN] Pipeline stop error: {e}")

        if self.mcp_manager:
            try:
                print("   Closing MCP connections...")
                await self.mcp_manager.disconnect_all()
                print("   [OK] MCP connections closed")
            except Exception as e:
                print(f"   [WARN] MCP disconnect error: {e}")

        if self.agent_pool:
            try:
                print("   Stopping AgentPool...")
                await self.agent_pool.shutdown_all()
                print("   [OK] AgentPool stopped")
            except Exception as e:
                print(f"   [WARN] AgentPool stop error: {e}")

        if self.broker_bridge:
            try:
                print("   Stopping BrokerBridge...")
                await self.broker_bridge.stop()
                print("   [OK] BrokerBridge stopped")
            except Exception as e:
                print(f"   [WARN] BrokerBridge stop error: {e}")

        if self.subagent_manager:
            try:
                print("   Stopping SubagentManager...")
                await self.subagent_manager.stop()
                print("   [OK] SubagentManager stopped")
            except Exception as e:
                print(f"   [WARN] SubagentManager stop error: {e}")

        if self.agent_bus:
            try:
                print("   Closing AgentMessageBus...")
                await self.agent_bus.close()
                print("   [OK] AgentMessageBus closed")
            except Exception as e:
                print(f"   [WARN] AgentMessageBus close error: {e}")

        if self.broker:
            try:
                print("   Stopping Broker...")
                await self.broker.stop()
                print("   [OK] Broker stopped")
            except Exception as e:
                print(f"   [WARN] Broker stop error: {e}")

        if self._auto_compact_task is not None:
            try:
                print("   Stopping AutoCompactService...")
                self._auto_compact_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._auto_compact_task
                print("   [OK] AutoCompactService stopped")
            except Exception as e:
                print(f"   [WARN] AutoCompactService stop error: {e}")

        # Close subagent memory systems
        for sub_name, sub_ms in getattr(self, "_subagent_memory_systems", {}).items():
            try:
                print(f"   Closing subagent memory system: {sub_name}...")
                await sub_ms.close()
                print(f"   [OK] Subagent memory system '{sub_name}' closed")
            except Exception as e:
                print(f"   [WARN] Subagent memory system '{sub_name}' close error: {e}")

        # Close peer memory systems
        for peer_name, peer_ms in getattr(self, "_peer_memory_systems", {}).items():
            try:
                print(f"   Closing peer memory system: {peer_name}...")
                await peer_ms.close()
                print(f"   [OK] Peer memory system '{peer_name}' closed")
            except Exception as e:
                print(f"   [WARN] Peer memory system '{peer_name}' close error: {e}")

        if self._overflow_cleaner is not None:
            try:
                print("   Stopping OverflowCleaner...")
                await self._overflow_cleaner.stop()
                print("   [OK] OverflowCleaner stopped")
            except Exception as e:
                print(f"   [WARN] OverflowCleaner stop error: {e}")

        if self.memory_system:
            try:
                print("   Closing MemorySystem...")
                await self.memory_system.close()
                print("   [OK] MemorySystem closed")
            except Exception as e:
                print(f"   [WARN] MemorySystem close error: {e}")

        if self.plugin_integration:
            try:
                print("   Shutting down plugin providers...")
                await self.plugin_integration.shutdown()
                print("   [OK] Plugin providers shut down")
            except Exception as e:
                print(f"   [WARN] Plugin shutdown error: {e}")

        if self._tasks:
            print(f"   Cancelling {len(self._tasks)} tasks...")
            for task in self._tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True), timeout=5.0
                )
            except TimeoutError:
                print("   [WARN] Some tasks did not stop in time")
            except asyncio.CancelledError:
                pass

        print("[OK] Bot Service stopped")
