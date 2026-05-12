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
from framework.control.channel import InMemoryControlChannel
from framework.control.ui.im import IMUserInterface
from framework.core.emitter import ContentEmitter
from framework.core.llm_error import (
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
from framework.hook.builtin import InboxFlushHook
from framework.interceptor.builtin import (
    ControlDrainInterceptor,
    ToolResultLimitInterceptor,
)
from framework.interceptor.builtin.tool_approval import ArgumentMatcher
from framework.interceptor.chain import InterceptorChain
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.agent import AgentConfig as IOCAgentConfig
from framework.ioc.configs.memory import MemoryConfig as IOCMemoryConfig
from framework.ioc.factories.llm import create_llm_provider
from framework.ioc.factories.memory import create_memory as ioc_create_memory
from framework.ioc.factories.tools import connect_mcp, register_mcp_tools, create_tool_manager
from framework.memory.context_governance import (
    CompositeGovernance,
    FinalContextLegalityGovernance,
    LossyContentCompactionGovernance,
    PriorityBudgetGovernance,
    ToolChainRepairGovernance,
)
from framework.memory.core.scope import MemoryContext
from framework.memory.injection import FullInjectionPolicy
from framework.memory.layers.config import (
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
)
from framework.memory.retention import DefaultMessageRetentionPolicy
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
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.server_local import LocalFileInboxServer
from framework.pipeline.adapters import InputAdapter, OutputAdapter
from framework.providers.litellm_provider import LiteLLMProvider

from .builders import AgentBuilderMixin

logger = logging.getLogger(__name__)


class BotService(AgentBuilderMixin):
    """Generic bot service supporting arbitrary InputAdapter/OutputAdapter pairs.

    Can be used for QQ, Discord, Feishu, DingTalk, Telegram, CLI, etc.
    Just provide the corresponding adapters and an Emitter factory.

    Modes:
    - pipeline: single AgentPipeline (default). SubagentManager spawns asyncio.Task.
    - pool: resident AgentPool with MessageBroker routing.

    Accepts either a legacy config dict or an IOC AppConfig object.
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
        legacy_raw: dict[str, Any] | None = None,
    ):
        self.config_dir = config_dir
        self.config_loader = ConfigLoader(config_dir)
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory
        self.mode = mode
        self._app_config = app_config
        self._legacy_raw = legacy_raw or {}

        # Build minimal legacy dict for remaining unconverted code paths
        # (MCP config, tools config). These will be removed in follow-up PRs.
        self.config: dict[str, Any] = self._build_legacy_config()

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
        """First agent in AppConfig agents list, if any."""
        if self._app_config and self._app_config.agents:
            return self._app_config.agents[0]
        return None

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

    def _get_llm_config(self) -> Any:
        """Get LLM config from IOC or fall back to dict."""
        if self._app_config is not None:
            return self._app_config.llm
        return self.config.get("llm", {})

    def _get_safety_config(self) -> Any:
        """Get safety config from IOC or fall back to dict."""
        if self._app_config is not None and self._app_config.safety is not None:
            return self._app_config.safety
        return self.config.get("runtime_safety", {})

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

        # 1. Load config (via IOC if available)
        if self._app_config is None:
            self._app_config = self._load_app_config()
        if not self.config:
            self.config = self._load_config()
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
            config=self._build_memory_layer_config(main_memory_config),
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

        # 7. Create AgentFactory (with runtime components)
        hook_runner = self._build_hook_runner(runtime_hooks)
        interceptor_chain = self._build_interceptor_chain()
        self.agent_factory = DefaultAgentFactory(
            default_llm_provider=provider,
            default_tool_manager=self.tool_manager,
            skill_manager=main_skill_manager,
            inbox_server=self.inbox_server,
            default_hooks=runtime_hooks,
            default_hook_runner=hook_runner,
            default_interceptor_chain=interceptor_chain,
        )

        # 7.5. Initialize approval infrastructure
        approval_cfg = self.config.get("approval", {})
        self._approval_workspace = self._project_dir / approval_cfg.get(
            "workspace", "data/approval"
        )
        from framework.runtime.store import NoOpTurnStateStore
        self._turn_store = NoOpTurnStateStore()
        self._im_ui = IMUserInterface(
            output_adapter=self.output_adapter,
            channel=self.control_channel,
        )
        print(f"[OK] Approval infrastructure initialized (workspace: {self._approval_workspace})")

        # 7.6. Initialize typed runtime stores (TurnStateStore + RuntimeCommandStore)
        from framework.runtime.codec import RuntimeStateCodecRegistry
        from framework.runtime.enums import AgentKind
        from framework.agents.react.state import ReActRuntimeStateCodec
        from framework.runtime.store import JsonFileTurnStateStore, JsonFileRuntimeCommandStore

        runtime_data_dir = self._project_dir / "data" / "runtime_state"
        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        self._turn_store = JsonFileTurnStateStore(runtime_data_dir / "turns", codec_registry)
        self._command_store = JsonFileRuntimeCommandStore(runtime_data_dir / "commands")
        print(f"[OK] Typed runtime stores initialized (data/runtime_state/)")

        # 8. Create ReActAgent (main agent in full mode with approval)
        self.agent = ReActAgent(provider=provider, mode="full")
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

        # Build AgentRuntime via framework RuntimeAssembler
        runtime = await self._assemble_runtime(hooks=self._build_hook_runner(pipeline_hooks))

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
            hook_runner=self._build_hook_runner(pipeline_hooks),
            interceptor_chain=self.interceptor_chain,
            subagent_manager=self.subagent_manager,
            context_manager_factory=self._get_context_manager,
            governance=self._build_governance(),
            safety=self.safety_policy,
            approval_workspace=str(self._approval_workspace),
            user_interface=self._im_ui,
            turn_store=self._turn_store,
            command_store=self._command_store,
            runtime_services=runtime.services,
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
            print("[OK] Main agent pool pipeline wired with AgentRuntime services")

        # Register subagents as residents (pool mode requires all targets to be resident)
        subagent_memory_config = self.config.get("memory", {}).get("subagents", {})
        for sub_key in ("subagent_sync", "subagent"):
            sub_config = multi_agent_config.get(sub_key, {})
            if not sub_config or not sub_config.get("enabled", True):
                continue
            descriptor, _, _ = await self._build_subagent_descriptor(sub_config)
            if descriptor.address.name != parent_agent_name:
                await self.agent_pool.register_resident(descriptor)
                # Inject lightweight governance into subagent pipeline
                sub_instance = self.agent_pool.get(descriptor.address.name)
                if sub_instance and sub_instance.pipeline:
                    sub_instance.pipeline.governance = self._build_peer_governance(subagent_memory_config)
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

    def _build_legacy_config(self) -> dict[str, Any]:
        """Build minimal dict for remaining legacy code paths from IOC config."""
        if self._app_config is None:
            return {}

        _cfg = self._app_config
        _main = _cfg.agents[0] if _cfg.agents else None
        _mem = _main.memory if _main else None

        return {
            "llm": _cfg.llm.model_dump(),
            "agent": {
                "system_prompt": _main.system_prompt if _main else "",
                "max_iterations": _main.max_steps if _main else 20,
                "approval": (
                    _main.approval.model_dump() if _main and _main.approval
                    else {"enabled": False, "tools": {}}
                ),
            },
            "multi_agent": {
                "enabled": len(_cfg.agents) > 1,
                "parent_agent_name": _main.name if _main else "main",
                "peers": [
                    {"name": a.name, "system_prompt": a.system_prompt}
                    for a in _cfg.agents[1:]
                ],
            },
            "memory": {
                "main": {
                    "short_term": {
                        "max_messages": _mem.short_term.max_messages if _mem else 100,
                        "max_tokens": _mem.short_term.max_tokens if _mem else 100000,
                        "keep_ratio_for_messages": _mem.short_term.keep_ratio_for_messages if _mem else 0.4,
                        "keep_ratio_for_token": _mem.short_term.keep_ratio_for_token if _mem else 0.4,
                        "auto_llm_compression": _mem.short_term.auto_llm_compression if _mem else True,
                    },
                    "retention": {
                        "anchors": {
                            "min_recent_user_turns": _mem.retention.min_recent_user_turns if _mem else 2,
                            "min_recent_agent_turns": _mem.retention.min_recent_agent_turns if _mem else 1,
                        },
                    },
                    "long_term": {"enabled": _mem is not None and _mem.long_term is not None and _mem.long_term.enabled},
                    "dream_engine": {"enabled": _mem is not None and _mem.dream_engine is not None and _mem.dream_engine.enabled},
                    "governance": self._governance_to_dict(_mem.governance if _mem else None),
                },
                "peers": {"short_term": {"max_messages": 50, "max_tokens": 50000}},
                "subagents": {"short_term": {"max_messages": 50, "max_tokens": 50000}},
            },
            "mcp": self._legacy_raw.get("mcp", {}),
            "tools": {"file_tools": {"enabled": True}, "shell_tools": {"enabled": True}, "search_tools": {"enabled": True}},
            "paths": _cfg.paths.model_dump(),
        }

    @staticmethod
    def _governance_to_dict(g: object | None) -> dict[str, object]:
        if g is None:
            return {"enabled": False}
        from framework.ioc.configs.memory import GovernanceConfig
        if not isinstance(g, GovernanceConfig):
            return {"enabled": False}
        return {
            "enabled": True,
            "tool_chain_repair": g.tool_chain_repair,
            "token_budget": (
                {"enabled": True, "budget_ratio": g.token_budget.budget_ratio,
                 "safety_buffer": g.token_budget.safety_buffer}
                if g.token_budget else {"enabled": False}
            ),
            "lossy_compaction": (
                {"enabled": True, "tool_result_head_chars": g.lossy_compaction.tool_result_head_chars,
                 "assistant_head_chars": g.lossy_compaction.assistant_head_chars}
                if g.lossy_compaction else {"enabled": False}
            ),
        }

    @property
    def safety_policy(self) -> RuntimeSafetyPolicy:
        """Safety policy from IOC config or fallback."""
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
            rs = self.config.get("runtime_safety", {})
            lc = rs.get("llm", {})
            tc = rs.get("turn", {})
            backoff = lc.get("retry_backoff_seconds", [2.0, 8.0])
            if isinstance(backoff, list):
                backoff = tuple(backoff)
            policy = RuntimeSafetyPolicy(
                llm=LLMTimeoutPolicy(
                    request_timeout_seconds=lc.get("request_timeout_seconds", 45.0),
                    stream_idle_timeout_seconds=lc.get("stream_idle_timeout_seconds", 90.0),
                    framework_max_retries=lc.get("framework_max_retries", 1),
                    retry_backoff_seconds=backoff,
                ),
                turn=TurnTimeoutPolicy(
                    agent_run_timeout_seconds=tc.get("agent_run_timeout_seconds", 180.0),
                    hook_timeout_seconds=tc.get("hook_timeout_seconds", 10.0),
                    tool_timeout_seconds=tc.get("tool_timeout_seconds", 60.0),
                ),
            )
        self._safety_policy_cache = policy
        return policy

    def _create_provider(self) -> LiteLLMProvider:
        """Create LLM Provider from IOC config."""
        if self._app_config is not None:
            return create_llm_provider(self._app_config.llm, self._app_config.safety)

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
            from framework.hook.builtin import RunLoggingHook

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

        # 2. Tool result limit – prevents excessively long context
        tools_config = self.config.get("tools", {})
        max_result_chars = tools_config.get("max_tool_result_chars", 8000)
        chain.add(ToolResultLimitInterceptor(max_chars=max_result_chars))

        self.interceptor_chain = chain
        return chain

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

        # Read agent-level approval config from bot_config.yml
        agent_config = self.config.get("agent", {})
        approval_raw = agent_config.get("approval", {})
        enabled = approval_raw.get("enabled", True)
        tools_raw = approval_raw.get("tools", {})

        tools_approval: dict[str, ToolApprovalConfig] = {}
        for tool_name, tool_cfg in tools_raw.items():
            if isinstance(tool_cfg, dict):
                tools_approval[tool_name] = ToolApprovalConfig(
                    allowed_paths=tool_cfg.get("allowed_paths", [])
                )

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
            governance=self._build_governance(),
            safety=self.safety_policy,
        ))
        print(f"[OK] AgentRuntime built (approval enabled={enabled}, tools={list(tools_approval.keys())})")
        return runtime

    # ------------------------------------------------------------------ #
    # Memory helpers
    # ------------------------------------------------------------------ #

    def _build_memory_layer_config(self, main_memory_config: dict[str, Any]) -> MemoryLayerConfigSet:
        """Build MemoryLayerConfigSet from legacy dict (prefer IOC if available)."""
        _mem = self._main_memory_cfg
        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(
                max_messages=(
                    _mem.short_term.max_messages if _mem
                    else main_memory_config.get("short_term", {}).get("max_messages", 100)
                ),
            ),
            pending=(
                PendingPrunedInputMemoryConfig(
                    enabled=_mem.pending.enabled,
                    max_entries=_mem.pending.max_entries,
                    max_chars=_mem.pending.max_chars,
                )
                if _mem
                else PendingPrunedInputMemoryConfig(
                    enabled=True,
                    max_entries=main_memory_config.get("pending_pruned_inputs", {}).get("max_entries", 8),
                    max_chars=main_memory_config.get("pending_pruned_inputs", {}).get("max_chars", 12000),
                )
            ),
        )

    def _build_governance(self) -> Any | None:
        """Build ContextGovernance chain (IOC preferred)."""
        _mem = self._main_memory_cfg
        _gov = _mem.governance if _mem else None

        # Fallback to dict
        if _gov is None:
            memory_config = self.config.get("memory", {})
            main_memory = memory_config.get("main", {})
            gov_config = main_memory.get("governance", {})
            if not gov_config.get("enabled", True):
                return None
        elif not _gov.tool_chain_repair:
            return None

        strategies: list[Any] = []
        strategies.append(ToolChainRepairGovernance())

        # Token budget
        if _gov is not None and _gov.token_budget is not None:
            tb = _gov.token_budget
            retention_policy = DefaultMessageRetentionPolicy.from_config({})
            strategies.append(
                PriorityBudgetGovernance(
                    max_tokens=min(int(80000 * tb.budget_ratio), 128000),
                    safety_buffer=tb.safety_buffer,
                    retention_policy=retention_policy,
                )
            )
        elif _gov is None:
            # Legacy fallback
            memory_config = self.config.get("memory", {})
            main_memory = memory_config.get("main", {})
            gov_config = main_memory.get("governance", {})
            if gov_config.get("token_budget", {}).get("enabled", True):
                llm_max_tokens = self.config.get("llm", {}).get("max_tokens", 80000)
                tb_cfg = gov_config.get("token_budget", {})
                budget_ratio = tb_cfg.get("budget_ratio", 0.5)
                max_tokens = min(int(llm_max_tokens * budget_ratio), 128000)
                strategies.append(
                    PriorityBudgetGovernance(
                        max_tokens=max_tokens,
                        safety_buffer=tb_cfg.get("safety_buffer", 1024),
                        retention_policy=DefaultMessageRetentionPolicy.from_config({}),
                        min_recent_user_turns=1,
                        min_recent_agent_turns=1,
                    )
                )

        # Lossy compaction
        if _gov is not None and _gov.lossy_compaction is not None:
            lc = _gov.lossy_compaction
            strategies.append(
                LossyContentCompactionGovernance(
                    tool_result_head_chars=lc.tool_result_head_chars,
                    assistant_head_chars=lc.assistant_head_chars,
                )
            )
        elif _gov is None:
            # Legacy fallback
            memory_config = self.config.get("memory", {})
            main_memory = memory_config.get("main", {})
            gov_config = main_memory.get("governance", {})
            lossy_cfg = gov_config.get("lossy_compaction", {})
            if lossy_cfg.get("enabled", True):
                strategies.append(
                    LossyContentCompactionGovernance(
                        tool_result_head_chars=lossy_cfg.get("tool_result_head_chars", 1200),
                        assistant_head_chars=lossy_cfg.get("assistant_head_chars", 1200),
                    )
                )

        strategies.append(FinalContextLegalityGovernance())
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
        """Build compression coordinator (IOC preferred).

        max_tokens for compression trigger: uses IOC ShortTermConfig.max_tokens
        (default 100000) or legacy dict fallback.
        """
        _mem = self._main_memory_cfg
        short_term = main_memory_config.get("short_term", {})

        st_max_messages = _mem.short_term.max_messages if _mem else short_term.get("max_messages", 100)
        st_max_tokens = _mem.short_term.max_tokens if _mem else short_term.get("max_tokens", 100000)
        st_keep_ratio_msg = _mem.short_term.keep_ratio_for_messages if _mem else short_term.get("keep_ratio_for_messages", 0.4)
        st_keep_ratio_token = _mem.short_term.keep_ratio_for_token if _mem else short_term.get("keep_ratio_for_token", 0.4)
        auto_compression = (
            _mem.short_term.auto_llm_compression if _mem
            else short_term.get("auto_llm_compression", True)
        )

        auto_compact = main_memory_config.get("auto_compact", {})
        if not auto_compression and not auto_compact.get("enabled", True):
            return None

        from framework.agents.summarizer import SummarizerAgent, SummarizerStrategy
        from framework.memory.compaction.boundary import (
            BoundaryPolicyName,
            create_boundary_policy,
        )
        from framework.memory.compaction.policy import ConservativeCompactionPolicy
        from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator

        self._summarizer_agent = SummarizerAgent(self.provider)
        summary_strategy = SummarizerStrategy(self._summarizer_agent)

        compaction_config = main_memory_config.get("compaction", {})
        compaction = ConservativeCompactionPolicy(
            high_value_tools=set(compaction_config.get("high_value_tools", []))
        )

        retention_policy = DefaultMessageRetentionPolicy.from_config(
            main_memory_config.get("retention", {})
        )

        boundary_name = BoundaryPolicyName(
            compaction_config.get("boundary", BoundaryPolicyName.TOOL_CHAIN.value)
        )
        boundary = create_boundary_policy(boundary_name)

        return DefaultMemoryCompressionCoordinator(
            summary=summary_strategy,
            compaction=compaction,
            boundary=boundary,
            retention=retention_policy,
            max_messages=st_max_messages,
            max_tokens=st_max_tokens,
            keep_ratio_for_messages=st_keep_ratio_msg,
            keep_ratio_for_token=st_keep_ratio_token,
        )

    async def _init_auto_compact(
        self,
        main_memory_config: dict[str, Any],
        compression_coordinator: Any | None,
    ) -> None:
        """Initialize and start background auto-compact via DefaultMemoryMaintenancePolicy."""
        ac_config = main_memory_config.get("auto_compact", {})
        if not ac_config.get("enabled", False):
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
