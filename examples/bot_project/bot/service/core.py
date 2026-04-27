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
from typing import Any, Literal

from bot.plugins.integration import PluginIntegration
from bot.utils.config_loader import ConfigLoader, validate_config
from framework import (
    AgentPipeline,
    InMemoryToolManager,
    ReActAgent,
    ToolManagerConfig,
)
from framework.core.emitter import ContentEmitter
from framework.core.llm_error import (
    CircuitBreakerPolicy,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from framework.core.skills import (
    FileSkillSource,
    ProgressiveBuilder,
    ResolutionContext,
    SkillManager,
)
from framework.extensions.llm.litellm_provider import LiteLLMProvider
from framework.memory.context_governance import (
    CompositeGovernance,
    MicrocompactGovernance,
    TokenBudgetGovernance,
    ToolChainRepairGovernance,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.injection import FullInjectionPolicy
from framework.memory.system import (
    MemorySystemContextManager,
    create_memory_system,
)
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
from framework.multi_agent.inbox.hook import InboxFlushHook
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.pipeline.adapters import InputAdapter, OutputAdapter

from .builders import AgentBuilderMixin

logger = logging.getLogger(__name__)


class BotService(AgentBuilderMixin):
    """Generic bot service supporting arbitrary InputAdapter/OutputAdapter pairs.

    Can be used for QQ, Discord, Feishu, DingTalk, Telegram, CLI, etc.
    Just provide the corresponding adapters and an Emitter factory.

    Modes:
    - pipeline: single AgentPipeline (default). SubagentManager spawns asyncio.Task.
    - pool: resident AgentPool with MessageBroker routing.
    """

    def __init__(
        self,
        config_dir: Path,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Callable[[str], ContentEmitter],
        mode: Literal["pipeline", "pool"] = "pipeline",
        config: dict[str, Any] | None = None,
    ):
        self.config_dir = config_dir
        self.config_loader = ConfigLoader(config_dir)
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory
        self.mode = mode
        self.config: dict[str, Any] = config or {}

        # Components
        self.pipeline: AgentPipeline | None = None
        self.agent_pool: AgentPool | None = None
        self.broker_bridge: BrokerBridgeService | None = None
        self.agent_bus: Any | None = None
        self.tool_manager: InMemoryToolManager | None = None
        self.mcp_manager: Any | None = None
        # TODO: Restore proper types after memory system migration
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

        # Runtime components (initialized during initialize())
        self.provider: Any | None = None
        self.plugin_integration: Any | None = None
        self.dream_engine: Any | None = None

        # Subagent skill-manager cache
        self._subagent_skill_managers: dict[str, SkillManager] = {}

        # Subagent memory-system cache (lazy-loaded)
        self._subagent_memory_systems: dict[str, Any] = {}

        # Peer memory-system cache (lazy-loaded)
        self._peer_memory_systems: dict[str, Any] = {}

        # Auto-compact background task
        self._auto_compact_task: asyncio.Task | None = None

        # RuntimeSafetyPolicy cache
        self._safety_policy_cache: RuntimeSafetyPolicy | None = None
        self._safety_policy_cache_key: int | None = None

        # Runtime control
        self._shutdown_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ #
    # Path helpers
    # ------------------------------------------------------------------ #

    @property
    def _project_dir(self) -> Path:
        """Project root directory (where bot_service.py lives)."""
        return Path(__file__).parent.parent.parent

    def _resolve_path(self, config_key: str, default_relative: str) -> Path:
        """Resolve a path from config paths: section, falling back to a relative default."""
        paths_config = self.config.get("paths", {})
        config_value = paths_config.get(config_key)
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

        # 1. Load config
        if not self.config:
            self.config = self._load_config()
        validation_warnings = validate_config(self.config)
        if validation_warnings:
            for w in validation_warnings:
                logger.error("Config validation: %s", w)
            raise RuntimeError(
                f"Config validation failed: {len(validation_warnings)} errors"
            )
        print("[OK] Config loaded")

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
        local_plugins_dir = Path(__file__).parent.parent.parent / "plugins"
        self.plugin_integration = PluginIntegration(
            self.config,
            extra_plugin_dirs=[local_plugins_dir] if local_plugins_dir.exists() else [],
        )
        has_plugins = await self.plugin_integration.discover_and_load()
        if has_plugins:
            self.plugin_integration.inject_tools(self.tool_manager)
            print(f"[OK] Plugin system loaded, {len(self.plugin_integration.list_plugins())} plugins active")
        else:
            print("[INFO] No plugins found")

        # 4. Create LLM Provider
        provider = self._create_provider()
        self.provider = provider
        llm_config = self.config["llm"]
        print(f"[OK] LLM Provider: {llm_config['model']}")

        # 5. Initialize MemorySystem using new registry-backed architecture
        data_dir = self._resolve_path("data_dir", "data")
        memory_dir = self._resolve_path("memory_dir", str(Path(data_dir) / "memory"))
        memory_dir.mkdir(parents=True, exist_ok=True)
        memory_config = self.config.get("memory", {})
        main_memory_config = memory_config.get("main", {})
        compression_coordinator = self._build_compression_coordinator(main_memory_config)
        lifecycle_policy = None
        if compression_coordinator is not None:
            from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy

            lifecycle_policy = DefaultMemoryLifecyclePolicy(
                compression_coordinator=compression_coordinator
            )
        self.memory_system = create_memory_system(
            workspace=memory_dir,
            llm_provider=self.provider,
            lifecycle_policy=lifecycle_policy,
        )
        await self.memory_system.initialize()

        self.context_manager = MemorySystemContextManager(
            memory_system=self.memory_system,
            default_agent_id=self.config.get("multi_agent", {}).get("parent_agent_name", "main"),
            default_agent_role="main",
            base_system_prompt=self.config["agent"]["system_prompt"],
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
        await self._init_long_term_defaults(data_dir, main_memory_config)
        await self._init_auto_compact(main_memory_config, compression_coordinator)
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
            builder = ProgressiveBuilder(base_path=self._project_dir)
            main_skill_manager = SkillManager(
                source=source, builder=builder
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

        # 7. Create AgentFactory
        self.agent_factory = DefaultAgentFactory(
            default_llm_provider=provider,
            default_tool_manager=self.tool_manager,
            skill_manager=main_skill_manager,
            inbox_server=self.inbox_server,
            default_hooks=runtime_hooks,
        )

        # 8. Create ReActAgent
        self.agent = ReActAgent(provider=provider)
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
        print(f"   Model: {llm_config['model']}")
        print(f"   max_tokens: {llm_config.get('max_tokens', 2000)}")
        print(f"   temperature: {llm_config.get('temperature', 0.7)}")

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

        multi_agent_config = self.config.get("multi_agent", {})
        parent_agent_name = multi_agent_config.get("parent_agent_name", "main")
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
        # TODO: Re-enable after memory system migration
        # if self.context_manager is None:
        #     raise RuntimeError("ContextManager is not initialized")
        if self.tool_manager is None:
            raise RuntimeError("ToolManager is not initialized")

        agent_config = self.config.get("agent", {})
        pipeline_hooks = [inbox_flush_hook]
        pipeline_hooks.extend(self._collect_run_hooks())

        self.pipeline = AgentPipeline(
            agent=self.agent,
            context_manager=self.context_manager,
            tool_manager=self.tool_manager,
            input_adapter=self.input_adapter,
            output_adapter=self.output_adapter,
            emitter_factory=self.emitter_factory,
            dream_engine=self.dream_engine,
            dream_interval=300,
            max_iterations=agent_config.get("max_iterations", 40),
            skill_manager=main_skill_manager,  # type: ignore[arg-type]
            hooks=pipeline_hooks,
            subagent_manager=self.subagent_manager,
            context_manager_factory=self._get_context_manager,
            governance=self._build_governance(),
            safety=self.safety_policy,
        )
        print("[OK] AgentPipeline initialized")
        print(f"   Input: {self.input_adapter.name}")
        print(f"   Output: {self.output_adapter.name}")

    async def _initialize_pool(self, _main_skill_manager: SkillManager | None) -> None:
        """Initialize pool-mode runtime."""
        from framework.multi_agent.bus import LocalAgentMessageBus

        if self.broker is None:
            raise RuntimeError("Broker is not initialized")
        if self.agent_factory is None:
            raise RuntimeError("AgentFactory is not initialized")
        if self.inbox_producer is None:
            raise RuntimeError("InboxProducer is not initialized")
        if self.inbox_consumer is None:
            raise RuntimeError("InboxConsumer is not initialized")

        multi_agent_config = self.config.get("multi_agent", {})
        parent_agent_name = multi_agent_config.get("parent_agent_name", "main")
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

        parent_name = multi_agent_config.get("parent_agent_name", "main")
        self.agent_pool = AgentPool(
            broker=self.broker,
            agent_factory=self.agent_factory,
            default_context_manager=self.context_manager,
            agent_bus=self.agent_bus,
            inbox_consumer=self.inbox_consumer,
            enable_inbox_polling=multi_agent_config.get("enable_inbox_polling", True),
            inbox_poll_interval=multi_agent_config.get("inbox_poll_interval", 10.0),
            default_context_manager_factory=self._get_context_manager,
            session_strategy=DefaultSessionIdStrategy(main_agent_name=parent_name),
            safety=self.safety_policy,
        )

        # Register main agent as resident
        main_descriptor = AgentDescriptor(
            address=main_address,
            llm_config=AgentLLMConfig(
                model=self.config["llm"].get("model"),
                temperature=self.config["llm"].get("temperature", 0.7),
                max_tokens=self.config["llm"].get("max_tokens", 2000),
            ),
            system_prompt_template=self.config["agent"].get("system_prompt", ""),
            context_strategy="persistent",
            max_iterations=self.config["agent"].get("max_iterations", 20),
            execution_strategy="react",
            safety_policy=self.safety_policy,
        )
        await self.agent_pool.register_resident(main_descriptor)
        print(f"[OK] AgentPool initialized, main agent '{parent_agent_name}' registered as resident")

        # Register subagents as residents (pool mode requires all targets to be resident)
        for sub_key in ("subagent_sync", "subagent"):
            sub_config = multi_agent_config.get(sub_key, {})
            if not sub_config or not sub_config.get("enabled", True):
                continue
            descriptor, _, _ = await self._build_subagent_descriptor(sub_config)
            if descriptor.address.name != parent_agent_name:
                await self.agent_pool.register_resident(descriptor)
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

    def _load_config(self) -> dict[str, Any]:
        """Load all configuration files."""
        config = self.config_loader.load_yaml("bot_config.yml")
        mcp_config = self.config_loader.load_mcp_config(config.get("mcp", {}))
        config["mcp"] = mcp_config
        return config

    @property
    def safety_policy(self) -> RuntimeSafetyPolicy:
        """Cached RuntimeSafetyPolicy built from bot_config.yml.

        Cache is invalidated if the config dict itself changes (e.g. hot reload).
        """
        cache_key = id(self.config)
        if self._safety_policy_cache is not None and self._safety_policy_cache_key == cache_key:
            return self._safety_policy_cache
        policy = self._build_safety_policy()
        self._safety_policy_cache = policy
        self._safety_policy_cache_key = cache_key
        return policy

    def _build_safety_policy(self) -> RuntimeSafetyPolicy:
        """Build RuntimeSafetyPolicy from bot_config.yml runtime_safety section."""
        rs_config = self.config.get("runtime_safety", {})
        llm_config = rs_config.get("llm", {})
        cb_config = rs_config.get("circuit_breaker", {})
        turn_config = rs_config.get("turn", {})
        backoff = llm_config.get("retry_backoff_seconds", [2.0, 8.0])
        if isinstance(backoff, list):
            backoff = tuple(backoff)
        return RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(
                request_timeout_seconds=llm_config.get("request_timeout_seconds", 45.0),
                stream_idle_timeout_seconds=llm_config.get("stream_idle_timeout_seconds", 90.0),
                framework_max_retries=llm_config.get("framework_max_retries", 1),
                retry_backoff_seconds=backoff,
            ),
            circuit_breaker=CircuitBreakerPolicy(
                enabled=cb_config.get("enabled", True),
                failure_threshold=cb_config.get("failure_threshold", 3),
                cooldown_seconds=cb_config.get("cooldown_seconds", 120.0),
            ),
            turn=TurnTimeoutPolicy(
                agent_run_timeout_seconds=turn_config.get("agent_run_timeout_seconds", 180.0),
                dispatch_timeout_seconds=turn_config.get("dispatch_timeout_seconds", 300.0),
                output_send_timeout_seconds=turn_config.get("output_send_timeout_seconds", 20.0),
                memory_flush_timeout_seconds=turn_config.get("memory_flush_timeout_seconds", 30.0),
                hook_timeout_seconds=turn_config.get("hook_timeout_seconds", 10.0),
                tool_timeout_seconds=turn_config.get("tool_timeout_seconds", 60.0),
            ),
        )

    def _create_provider(self) -> LiteLLMProvider:
        """Create LLM Provider."""
        llm_config = self.config["llm"]
        return LiteLLMProvider(
            model=llm_config["model"],
            api_key=llm_config["api_key"],
            base_url=llm_config.get("base_url"),
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2000),
            safety=self.safety_policy,
        )

    def _collect_run_hooks(self) -> list[Any]:
        """Collect optional run hooks configured for this bot service."""
        hooks = self.plugin_integration.collect_hooks()
        observability_config = self.config.get("observability", {})
        run_logging = observability_config.get("run_logging", {})
        if run_logging.get("enabled", False):
            from framework.core.hooks import RunLoggingHook

            level_name = str(run_logging.get("level", "INFO")).upper()
            level = getattr(logging, level_name, logging.INFO)
            hooks.append(
                RunLoggingHook(
                    logger_name=run_logging.get("logger_name", "bot.run"),
                    level=level,
                    max_content_chars=run_logging.get("max_content_chars", 4000),
                    max_result_chars=run_logging.get("max_result_chars", 4000),
                )
            )
        return hooks

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
                dream_config = self.config.get("memory", {}).get("main", {}).get("dream_engine", {})
                dream_interval = dream_config.get("interval", 300)
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
        print(f"   Model: {self.config['llm']['model']}")
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
    # Memory helpers
    # ------------------------------------------------------------------ #

    def _build_governance(self) -> Any | None:
        """Build ContextGovernance chain from config."""
        memory_config = self.config.get("memory", {})
        main_memory = memory_config.get("main", {})
        gov_config = main_memory.get("governance", {})
        if not gov_config.get("enabled", True):
            return None

        strategies: list[Any] = []
        if gov_config.get("tool_chain_repair", True):
            strategies.append(ToolChainRepairGovernance())

        microcompact = gov_config.get("microcompact", {})
        if microcompact.get("enabled", True):
            strategies.append(
                MicrocompactGovernance(
                    keep_recent=microcompact.get("keep_recent", 10),
                )
            )

        token_budget = gov_config.get("token_budget", {})
        if token_budget.get("enabled", True):
            llm_max_tokens = self.config.get("llm", {}).get("max_tokens", 80000)
            budget_ratio = token_budget.get("budget_ratio", 0.5)
            max_tokens = int(llm_max_tokens * budget_ratio)
            cap = 128000
            if max_tokens > cap:
                max_tokens = cap
            strategies.append(
                TokenBudgetGovernance(
                    max_tokens=max_tokens,
                    safety_buffer=token_budget.get("safety_buffer", 1024),
                )
            )

        if not strategies:
            return None
        return CompositeGovernance(strategies)

    async def _init_long_term_defaults(
        self,
        _data_dir: Path,
        main_memory_config: dict[str, Any],
    ) -> None:
        """Initialize default long-term memory files if enabled and not present."""
        lt_config = main_memory_config.get("long_term", {})
        if not lt_config.get("init_defaults", True):
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

    def _build_compression_coordinator(self, main_memory_config: dict[str, Any]) -> Any | None:
        short_term = main_memory_config.get("short_term", {})
        auto_compact = main_memory_config.get("auto_compact", {})
        if not short_term.get("auto_llm_compression", True) and not auto_compact.get("enabled", True):
            return None

        from framework.agents.summarizer import SummarizerAgent, SummarizerStrategy
        from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator

        # Shared SummarizerAgent for compression + consolidation + DreamEngine
        self._summarizer_agent = SummarizerAgent(self.provider)
        summary_strategy = SummarizerStrategy(self._summarizer_agent)

        return DefaultMemoryCompressionCoordinator(
            max_messages=auto_compact.get("max_messages", short_term.get("max_messages", 100)),
            max_tokens=auto_compact.get("max_tokens", short_term.get("max_tokens", 8000)),
            summary=summary_strategy,
        )

    async def _init_auto_compact(
        self,
        main_memory_config: dict[str, Any],
        compression_coordinator: Any | None,
    ) -> None:
        """Initialize and start background auto-compact via DefaultMemoryMaintenancePolicy."""
        ac_config = main_memory_config.get("auto_compact", {})
        if not ac_config.get("enabled", True):
            return
        if self.memory_system is None or compression_coordinator is None:
            return

        from framework.memory.lifecycle import DefaultMemoryMaintenancePolicy

        self._maintenance_policy = DefaultMemoryMaintenancePolicy(
            idle_threshold_seconds=ac_config.get("idle_threshold_seconds", 1800),
            keep_recent_messages=ac_config.get("keep_recent_messages", 8),
            compression_coordinator=compression_coordinator,
        )

        scan_interval = ac_config.get("scan_interval", 300)
        self._auto_compact_task = asyncio.create_task(
            self._auto_compact_loop(scan_interval)
        )
        print(
            f"   [OK] AutoCompactService started "
            f"(idle_threshold={ac_config.get('idle_threshold_seconds', 1800)}s, "
            f"scan_interval={scan_interval}s)"
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
        dream_config = self.config.get("memory", {}).get("main", {}).get("dream_engine", {})
        if not dream_config.get("enabled", False):
            return
        if self.memory_system is None or self.provider is None:
            return
        if self.memory_system.archive_manager is None or self.memory_system.knowledge_manager is None:
            return

        from framework.memory.consolidation.dream_engine import DreamEngine

        self.dream_engine = DreamEngine(
            llm_provider=self.provider,
            history_manager=self.memory_system.archive_manager,
            long_term_manager=self.memory_system.knowledge_manager,
            registry=self.memory_system.store_registry,
            max_batch_size=dream_config.get("max_batch_size", 20),
            max_iterations=dream_config.get("max_iterations", 10),
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
