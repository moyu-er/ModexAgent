"""BotService core — generic bot orchestration for any InputAdapter/OutputAdapter pair.

Supports two runtime modes:
- pipeline: single AgentPipeline, SubagentManager creates asyncio.Task directly.
- pool: AgentPool with resident agents, BrokerBridgeService routes messages.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from framework import (
    AgentPipeline,
    InMemoryToolManager,
    ReActAgent,
    ToolManagerConfig,
)
from framework.core.emitter import ContentEmitter
from framework.core.skills import (
    DependencyFilter,
    FileSkillSource,
    ProgressiveBuilder,
    ResolutionContext,
    SkillManager,
)
from framework.extensions.llm.litellm_provider import LiteLLMProvider
from framework.memory.content_transform import Base64SanitizeTransformer
from framework.memory.core.scope import PeerPairScope
from framework.memory.stores.file import FileStorage
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import (
    LayerConfig,
    MemorySystem,
    MemorySystemContextManager,
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
from bot.plugins.integration import PluginIntegration
from bot.utils.config_loader import ConfigLoader, validate_config

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
        self.memory_system: MemorySystem | None = None
        self.peer_memory_system: MemorySystem | None = None
        self.peer_context_manager: MemorySystemContextManager | None = None
        self.context_manager: MemorySystemContextManager | None = None
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

        # 5. Create MemorySystem and ContextManager (user<->main uses SessionScope)
        data_dir = self._resolve_path("memory_dir", "data/memory")
        memory_config = self.config.get("memory", {})
        short_term_config = memory_config.get("short_term", {})
        main_layers = MemorySystem.default_single_user_layers(
            workspace=data_dir,
            llm_provider=provider,
            auto_llm_compression=True,
            short_term_max_messages=short_term_config.get("max_messages"),
            short_term_max_tokens=short_term_config.get("max_tokens"),
            llm_max_tokens=self.config.get("llm", {}).get("max_tokens"),
            budget_ratio=short_term_config.get("budget_ratio", 0.5),
        )
        main_layers["short_term"].content_transformer = Base64SanitizeTransformer()
        self.memory_system = MemorySystem(
            workspace=data_dir,
            layers=main_layers,
            llm_provider=provider,
            auto_llm_compression=True,
        )
        await self.memory_system.initialize()

        # Apply plugin MemorySystem modifiers
        modifiers = self.plugin_integration.inject_memory_system_modifiers(self.memory_system)
        if modifiers:
            print(f"[OK] Applied {len(modifiers)} MemorySystem modifiers")

        self.context_manager = MemorySystemContextManager(
            memory_system=self.memory_system,
            base_system_prompt=self.config["agent"]["system_prompt"],
        )
        print(f"[OK] MemorySystem initialized (storage: {data_dir})")

        # 5.0 Shared Peer Communication MemorySystem
        peer_shared_dir = data_dir / "peer_shared"
        peer_shared_dir.mkdir(parents=True, exist_ok=True)
        peer_file_store = FileStorage(peer_shared_dir)
        self.peer_memory_system = MemorySystem(
            workspace=peer_shared_dir,
            llm_provider=provider,
            auto_llm_compression=True,
            layers={
                "working": LayerConfig(scope=PeerPairScope(), storage=InMemoryStorage()),
                "short_term": LayerConfig(
                    scope=PeerPairScope(),
                    storage=peer_file_store,
                    max_messages=short_term_config.get("max_messages", 50),
                    max_tokens=short_term_config.get("max_tokens", 80000),
                    content_transformer=Base64SanitizeTransformer(),
                ),
            },
        )
        await self.peer_memory_system.initialize()
        self.plugin_integration.inject_memory_system_modifiers(self.peer_memory_system)
        self.peer_context_manager = MemorySystemContextManager(
            memory_system=self.peer_memory_system,
            base_system_prompt=self.config["agent"]["system_prompt"],
        )
        print(f"[OK] Peer Shared MemorySystem initialized (storage: {peer_shared_dir})")

        # -- Inject plugin Memory Providers --
        await self.plugin_integration.inject_memory_providers(
            self.memory_system,
            init_kwargs={
                "llm_provider": self.provider,
                "workspace": data_dir,
            },
        )
        print("[OK] Plugin memory providers injected")

        # 5.1 Create DreamEngine
        from framework.memory.consolidation.dream_engine import DreamEngine

        history_mgr = self.memory_system.history_manager
        long_term_mgr = self.memory_system.long_term_manager
        if history_mgr is not None and long_term_mgr is not None:
            self.dream_engine = DreamEngine(
                llm_provider=provider,
                history_manager=history_mgr,
                long_term_manager=long_term_mgr,
            )
            print("[OK] DreamEngine initialized")
        else:
            self.dream_engine = None
            print("[INFO] DreamEngine skipped (history/long-term layer unavailable)")

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
            skill_filter = DependencyFilter(mode="filter")
            builder = ProgressiveBuilder(base_path=self._project_dir)
            main_skill_manager = SkillManager(
                source=source, skill_filter=skill_filter, builder=builder
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

        # Collect plugin hooks for factory injection
        plugin_hooks = self.plugin_integration.collect_hooks()

        # 7. Create AgentFactory
        self.agent_factory = DefaultAgentFactory(
            default_llm_provider=provider,
            default_tool_manager=self.tool_manager,
            skill_manager=main_skill_manager,
            inbox_server=self.inbox_server,
            default_hooks=plugin_hooks,
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
        if self.context_manager is None:
            raise RuntimeError("ContextManager is not initialized")
        if self.tool_manager is None:
            raise RuntimeError("ToolManager is not initialized")

        agent_config = self.config.get("agent", {})
        pipeline_hooks = [inbox_flush_hook]
        plugin_hooks = self.plugin_integration.collect_hooks()
        pipeline_hooks.extend(plugin_hooks)

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
        self.agent_pool = AgentPool(
            broker=self.broker,
            agent_factory=self.agent_factory,
            default_context_manager=self.context_manager,
            agent_bus=self.agent_bus,
            inbox_consumer=self.inbox_consumer,
            enable_inbox_polling=multi_agent_config.get("enable_inbox_polling", True),
            inbox_poll_interval=multi_agent_config.get("inbox_poll_interval", 10.0),
            default_context_manager_factory=self._get_context_manager,
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

    def _create_provider(self) -> LiteLLMProvider:
        """Create LLM Provider."""
        llm_config = self.config["llm"]
        return LiteLLMProvider(
            model=llm_config["model"],
            api_key=llm_config["api_key"],
            base_url=llm_config.get("base_url"),
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 2000),
        )

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
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            await self._shutdown_event.wait()
        except KeyboardInterrupt:
            print("\n[STOP] Shutting down...")
        finally:
            await self.stop()

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

        if self.memory_system:
            try:
                print("   Closing MemorySystem...")
                await self.memory_system.close()
                print("   [OK] MemorySystem closed")
            except Exception as e:
                print(f"   [WARN] MemorySystem close error: {e}")

        if self.peer_memory_system:
            try:
                print("   Closing Peer MemorySystem...")
                await self.peer_memory_system.close()
                print("   [OK] Peer MemorySystem closed")
            except Exception as e:
                print(f"   [WARN] Peer MemorySystem close error: {e}")

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
