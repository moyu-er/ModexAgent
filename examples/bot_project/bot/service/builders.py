"""Agent builder mixin for BotService — tool registration, memory, descriptors.

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from bot.plugins.integration import PluginIntegration
from framework import InMemoryToolManager, ToolManagerConfig, LLMProvider
from framework.core.context import ContextManager
from framework.core.skills import (
    CompositeSkillSource,
    DirectorySkillCache,
    FileSkillSource,
    DefaultSkillBuilder,
    SkillManager,
)
from framework.core.tool_manager import Tool
from framework.ioc.configs.app import AppConfig
from framework.memory.core.scope import MemoryAgentRole, MemoryContext, SessionScope
from framework.memory.injection import RestrictedInjectionPolicy
from framework.memory.pruned.manager import PrunedManager
from framework.memory.layers.config import (
    ArchiveMemoryConfig,
    MemoryLayerConfigSet,
    UserRetentionBufferConfig,
    SessionMemoryConfig,
)
from framework.memory.system import MemorySystemContextManager, create_memory_system
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentAddress,
    AgentPool,
    CommunicationTracker,
    AgentMessageBus,
)
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.tools import CommunicationTarget, CommunicationTargetStore, SendToAgentTool
from framework.pipeline.adapters import OutputAdapter
from framework.tools import MCPClientManager
from framework.tools.terminal import TerminalManagerBase

logger = logging.getLogger(__name__)


def resolve_system_prompt(agent_cfg: Any, project_dir: Path) -> str:
    """Resolve system prompt: agents/{name}.md if exists, else YAML value."""
    md_path = project_dir / "agents" / f"{agent_cfg.name}.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8")
    return getattr(agent_cfg, "system_prompt", "")


# ── Standard tool builders (code objects, no config) ──

def _make_file_tools() -> list[Tool]:
    from framework.tools.standard import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
    return [ReadFileTool(), WriteFileTool(), EditFileTool(), ListDirTool()]


def _make_shell_tool(
    terminal_manager: Any | None = None,
    timeout: int = 60,
) -> Tool:
    from framework.tools.terminal import SubprocessTool, SubprocessExecutor
    return SubprocessTool(executor=SubprocessExecutor(), timeout=timeout)


def _make_search_tools() -> list[Tool]:
    from framework.tools.standard import FindFilesTool, SearchFilesTool
    return [SearchFilesTool(), FindFilesTool()]


def _make_standard_tools() -> list[Tool]:
    return _make_file_tools() + [_make_shell_tool()] + _make_search_tools()


# ── MCP tool helpers ──

async def _load_agent_mcp_tools(
    agent_name: str,
    project_dir: Path,
) -> tuple[list[Tool], Any | None]:
    """Load MCP tools for an agent from config/mcp/{agent_name}.json.

    Returns (tools, mcp_manager) — the manager must be kept alive for
    connection lifecycle and disconnected on shutdown.
    """
    import json

    from framework.ioc.configs.app import _resolve_env_in
    from framework.tools.mcp import MCPClientManager
    from framework.tools.mcp_adapter import MCPToolAdapter
    from framework.tools.registry import ToolRegistry

    mcp_json = project_dir / "config" / "mcp" / f"{agent_name}.json"
    if not mcp_json.exists():
        return [], None

    try:
        with open(mcp_json, encoding="utf-8") as f:
            raw = json.load(f)

        servers = raw.get("mcpServers") or raw.get("servers") or {}
        if not servers:
            return [], None

        servers = _resolve_env_in(servers)
        manager = MCPClientManager(config=servers)
        await manager.initialize()

        if not manager.connected_servers:
            logger.warning("Agent %s: MCP config loaded but no servers connected", agent_name)
            return [], manager

        adapter = MCPToolAdapter(mcp_manager=manager, default_prefix=True, tool_timeout=60)
        registry = ToolRegistry()
        await adapter.register_tools(registry=registry)

        tools: list[Tool] = []
        for name in registry.list_tools():
            t = registry.get_tool(name)
            if t is not None:
                tools.append(t)
        logger.info("Agent %s: %d MCP tools loaded from %s", agent_name, len(tools), mcp_json.name)
        return tools, manager

    except Exception as e:
        logger.warning("Failed to load MCP tools for agent %s: %s", agent_name, e)
        return [], None


class AgentBuilderMixin:
    """Mixin providing tool registration, skill/memory management, and agent building.

    All fields below are provided by the host ``BotService`` class.
    They are declared here so the mixin's contract is visible to type checkers and IDEs.
    """

    # ── Fields provided by the host BotService class ──

    # Configuration
    _app_config: AppConfig | None
    mode: Literal["pipeline", "pool"]

    # Core components
    tool_manager: InMemoryToolManager | None
    output_adapter: OutputAdapter
    agent_pool: AgentPool | None
    broker: InMemoryMessageBroker | None
    agent_bus: AgentMessageBus | None

    communication_tracker: CommunicationTracker | None
    mcp_manager: MCPClientManager | None
    context_manager: ContextManager | None
    provider: LLMProvider | None
    plugin_integration: PluginIntegration | None
    pruned_manager: PrunedManager | None

    # Subagent caches
    _subagent_skill_managers: dict[str, SkillManager]
    _subagent_memory_systems: dict[str, Any]
    _additional_subagent_memory_systems: dict[str, Any]

    # ── Tool Registration (code-driven, no config dict) ──

    async def _register_tools(
        self, terminal_manager: TerminalManagerBase | None = None
    ) -> None:
        if self.tool_manager is None:
            return

        for tool in _make_file_tools():
            self.tool_manager.register(tool)

        if terminal_manager is not None:
            from framework.tools.terminal import TerminalTool, CommandTool, ProcessTool, ProcessRegistry
            from framework.tools.terminal.config import TerminalRuntimeConfig

            cfg = TerminalRuntimeConfig()
            registry = ProcessRegistry(config=cfg)
            self.tool_manager.register(CommandTool(manager=terminal_manager, registry=registry, config=cfg))
            self.tool_manager.register(ProcessTool(registry=registry, manager=terminal_manager))
            self.tool_manager.register(TerminalTool(terminal_manager))
        else:
            # No terminal backend — fall back to stateless subprocess
            shell_tool = _make_shell_tool(timeout=60)
            self.tool_manager.register(shell_tool)

        for tool in _make_search_tools():
            self.tool_manager.register(tool)
        print("   [OK] Standard tools registered (file + search)")

        from bot.tools.custom import SendFileToUserTool
        self.tool_manager.register(SendFileToUserTool(output_adapter=self.output_adapter))
        print("   [OK] send_file_to_user registered")

        # Experience tool is registered in BotService.initialize() alongside
        # the hook/curator so all three share the same ExperienceMetaStore.

    async def _register_mcp_tools(self) -> None:
        if self.tool_manager is None:
            return

        try:
            main_cfg = next(
                (a for a in self._app_config.agents if a.role == "main"),
                self._app_config.agents[0] if self._app_config.agents else None,
            )
            if main_cfg is None:
                return

            mcp_tools, self.mcp_manager = await _load_agent_mcp_tools(main_cfg.name, self._project_dir)
            for tool in mcp_tools:
                self.tool_manager.register(tool)

            if mcp_tools:
                logger.info("Registered %d MCP tools for main agent '%s'", len(mcp_tools), main_cfg.name)

        except ImportError as e:
            logger.warning("MCP adapter not available: %s", e)
        except Exception as e:
            logger.warning("MCP tools registration failed: %s", e)

    async def _register_multi_agent_tools(self) -> None:
        if self.tool_manager is None or self.broker is None:
            return

        agents = self._app_config.agents
        if len(agents) <= 1:
            return

        main_cfg = next((a for a in agents if a.role == "main"), agents[0] if agents else None)
        parent_name = main_cfg.name if main_cfg else "main"
        parent_address = AgentAddress(name=parent_name)

        strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)

        if self.agent_bus is not None:
            from framework.multi_agent.comm_kind import AgentCommKind
            from framework.multi_agent.communication import AgentCommunicationService
            comm_store = CommunicationTargetStore()
            # Populate from other configured agents
            for a in agents:
                if a.name != parent_name:
                    comm_store.add(CommunicationTarget(
                        name=a.name, kind=AgentCommKind.SUBAGENT,
                        description=getattr(a, "description", ""),
                    ))
            service = AgentCommunicationService(
                source=parent_address,
                broker=self.broker,
                registry=self.agent_pool,
                agent_bus=self.agent_bus,
                session_strategy=strategy,
                comm_tracker=self.communication_tracker,
                target_store=comm_store,
            )
            self.tool_manager.register(SendToAgentTool(
                store=comm_store,
                source=parent_address, broker=self.broker, registry=self.agent_pool,
                agent_bus=self.agent_bus, service=service,
                comm_tracker=self.communication_tracker,
            ))
            print("   [OK] send_to_agent registered")

    # ── Subagent Tool Manager (code-driven) ──

    async def _build_subagent_tool_manager(
        self,
        tools: list[Tool],
        agent_name: str | None = None,
    ) -> InMemoryToolManager:
        tm = InMemoryToolManager(config=ToolManagerConfig())
        for tool in tools:
            tm.register(tool)

        if agent_name:
            mcp_tools, _ = await _load_agent_mcp_tools(agent_name, self._project_dir)
            for tool in mcp_tools:
                tm.register(tool)

        return tm

    # ── Skill Management ──

    def _get_subagent_skill_manager(
        self, name: str, extra_dirs: list[Path] | None = None,
    ) -> SkillManager | None:
        cache_key = f"{name}:{':'.join(str(d) for d in extra_dirs)}" if extra_dirs else name
        if cache_key in self._subagent_skill_managers:
            return self._subagent_skill_managers[cache_key]

        sources: list[Any] = []

        default_dirs = [
            self._project_dir / "skills" / "subagents" / name,
        ]
        found_default = [d for d in default_dirs if d.exists()]
        if found_default:
            sources.append(FileSkillSource(
                directories=found_default, cache=True, layout="directory",
                skill_filename="SKILL.md",
            ))

        if extra_dirs:
            found_extra = [d for d in extra_dirs if d.exists()]
            if found_extra:
                sources.append(FileSkillSource(
                    directories=found_extra, cache=True, layout="flat",
                    skill_filename="SKILL.md",
                ))

        if not sources:
            return None

        source = (CompositeSkillSource(sources=sources, merge_strategy="last_wins")
                  if len(sources) > 1 else sources[0])
        builder = DefaultSkillBuilder(base_path=self._project_dir)

        all_dirs: list[Path] = []
        for s in sources:
            all_dirs.extend(s.directories)
        cache = DirectorySkillCache(
            directories=all_dirs,
            layout="directory",
        ) if sources else None

        mgr = SkillManager(source=source, builder=builder, cache=cache)
        self._subagent_skill_managers[cache_key] = mgr
        return mgr

    # ── Memory Creation ──

    def _build_memory_layer_config(self, cfg: Any) -> MemoryLayerConfigSet:
        from framework.ioc.configs.memory import MemoryConfig
        from framework.ioc.factories.memory import _build_memory_layer_config

        memory_cfg = cfg if isinstance(cfg, MemoryConfig) else MemoryConfig.model_validate(cfg)
        layer_config = _build_memory_layer_config(memory_cfg)
        if layer_config.user_retention is not None and not layer_config.user_retention.enabled:
            layer_config = replace(layer_config, user_retention=None)
        return layer_config

    def _session_only_memory_config(self, cfg: Any) -> MemoryLayerConfigSet:
        max_messages = 50
        if cfg is not None and hasattr(cfg, "session"):
            max_messages = cfg.session.max_messages
        elif cfg is not None and hasattr(cfg, "short_term"):
            max_messages = cfg.short_term.max_messages
        elif isinstance(cfg, dict):
            session = cfg.get("session", cfg.get("short_term", {}))
            max_messages = int(session.get("max_messages", max_messages))
        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=max_messages),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            knowledge=None,
            user_retention=UserRetentionBufferConfig(enabled=True),
        )

    async def _create_subagent_memory(self, sub_name: str, base_system_prompt: str = "") -> ContextManager:
        from framework.memory.core.scope import MemoryAgentRole

        subagent_cfg = self._find_subagent_cfg()
        sub_memory_cfg = subagent_cfg.memory if subagent_cfg else None
        data_dir = self.workspace_context.data_dir
        sub_dir = data_dir / "memory" / "subagents" / sub_name
        sub_dir.mkdir(parents=True, exist_ok=True)

        # Support both old (short_term) and new (session) config
        if sub_memory_cfg and hasattr(sub_memory_cfg, "session"):
            st = sub_memory_cfg.session
        elif sub_memory_cfg and hasattr(sub_memory_cfg, "short_term"):
            st = sub_memory_cfg.short_term
        else:
            st = None
        cleanup_config: dict[str, int | float] = {
            "max_messages": st.max_messages if st else 50,
            "max_tokens": st.max_tokens if st else 100000,
            "keep_ratio": st.keep_ratio_for_messages if st else 0.4,
        }

        # Inherit archive/knowledge agents from the main memory system so subagents
        # also generate archives and consolidate knowledge when enabled.
        archive_agent: Any | None = None
        knowledge_consolidator: Any | None = None
        if self.context_manager is not None:
            main_memory = getattr(self.context_manager, "memory_system", None)
            if main_memory is not None:
                archive_agent = getattr(main_memory, "_archive_agent", None)
                knowledge_consolidator = main_memory.knowledge_consolidator

        memory_system = create_memory_system(
            workspace=sub_dir,
            config=self._session_only_memory_config(sub_memory_cfg),
            session_only=False,
            cleanup_config=cleanup_config,
            pruned_manager=self.pruned_manager,
            archive_agent=archive_agent,
            archive_storage=None,
            knowledge_consolidator=knowledge_consolidator,
        )
        await memory_system.initialize()
        if self.plugin_integration:
            self.plugin_integration.inject_memory_system_modifiers(memory_system)
        self._subagent_memory_systems[sub_name] = memory_system
        return MemorySystemContextManager(
            memory_system=memory_system, default_agent_id=sub_name,
            default_agent_role=MemoryAgentRole.SUBAGENT,
            base_system_prompt=base_system_prompt,
            injection_policy=RestrictedInjectionPolicy(max_session_messages=20, pruned_manager=self.pruned_manager),
        )

    # ── Context Routing ──

    def _get_context_manager(self, session_id: str) -> ContextManager:
        if self.context_manager is None:
            raise RuntimeError("Context manager is not initialized")
        return self.context_manager

    # ── Cleanup ──

    async def _cleanup_subagent_memory(self, session_id: str) -> None:
        main_cfg = self._main_agent_cfg
        parent_name = main_cfg.name if main_cfg else "main"
        strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
        parts = strategy.parse(session_id)
        sub_name = parts.agent_name
        if sub_name is None:
            return
        memory_system = self._subagent_memory_systems.get(sub_name)
        if memory_system is None:
            return
        try:
            ctx = MemoryContext(session_id=session_id, user_id="default",
                                agent_id=sub_name, agent_role=MemoryAgentRole.SUBAGENT)
            await memory_system.clear(ctx)
            logger.info("Cleaned up subagent memory for session: %s", session_id)
        except Exception:
            logger.exception("Failed to clean up subagent memory for session: %s", session_id)

