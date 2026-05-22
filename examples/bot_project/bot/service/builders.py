"""Agent builder mixin for BotService — tool registration, memory, descriptors.

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from framework import InMemoryToolManager, ToolManagerConfig
from framework.core.context import ContextManager
from framework.core.skills import (
    CompositeSkillSource,
    DirectorySkillCache,
    FileSkillSource,
    ProgressiveBuilder,
    SkillManager,
)
from framework.core.tool_manager import Tool
from framework.ioc.factories.governance import create_subagent_governance
from framework.memory.core.scope import MemoryAgentRole, MemoryContext, SessionScope
from framework.memory.injection import RestrictedInjectionPolicy
from framework.memory.layers.config import (
    ArchiveMemoryConfig,
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
)
from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
from framework.memory.system import MemorySystemContextManager, create_memory_system
from framework.multi_agent import AgentAddress
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.tools import ListCommunicationTargetsTool, SendToAgentAsyncTool
from framework.pipeline.adapters import NullOutputAdapter

logger = logging.getLogger(__name__)

# ── Standard tool builders (code objects, no config) ──

def _make_file_tools() -> list[Tool]:
    from framework.tools.standard import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
    return [ReadFileTool(), WriteFileTool(), EditFileTool(), ListDirTool()]


def _make_shell_tool(timeout: int = 60, enable_safety_guard: bool = True) -> Tool:
    from framework.tools.standard import ShellTool
    return ShellTool(timeout=timeout, enable_safety_guard=enable_safety_guard)


def _make_search_tools() -> list[Tool]:
    from framework.tools.standard import FindFilesTool, SearchFilesTool
    return [SearchFilesTool(), FindFilesTool()]


def _make_standard_tools() -> list[Tool]:
    return _make_file_tools() + [_make_shell_tool()] + _make_search_tools()


# ── MCP tool helpers ──

async def _mcp_tools_for_agent(
    mcp_manager: Any,
    server_filter: list[str] | None,
) -> list[Tool]:
    """Get MCP tools for a specific agent, filtered by server names."""
    if mcp_manager is None or not server_filter:
        return []

    from framework.tools.mcp_adapter import MCPToolAdapter
    from framework.tools.registry import ToolRegistry

    adapter = MCPToolAdapter(mcp_manager=mcp_manager, default_prefix=True, tool_timeout=60)
    registry = ToolRegistry()
    await adapter.register_tools(registry=registry, server_filter=server_filter)
    return [registry.get(name) for name in registry.list_tools() if registry.get(name) is not None]


class AgentBuilderMixin:
    """Mixin providing tool registration, skill/memory management, and agent building."""

    # ── Tool Registration (code-driven, no config dict) ──

    async def _register_tools(self) -> None:
        if self.tool_manager is None:
            return

        for tool in _make_standard_tools():
            self.tool_manager.register(tool)
        print("   [OK] Standard tools registered (file + shell + search)")

        from bot.tools.custom import SendFileToUserTool
        self.tool_manager.register(SendFileToUserTool(output_adapter=self.output_adapter))
        print("   [OK] send_file_to_user registered")

    async def _register_mcp_tools(self) -> None:
        if self.tool_manager is None:
            return

        try:
            from framework.tools.mcp import MCPClientManager

            mcp_cfg = self._app_config.mcp
            if mcp_cfg is None or not mcp_cfg.servers:
                return

            logger.info("Connecting to %d MCP servers...", len(mcp_cfg.servers))
            servers_dict = {
                name: entry.model_dump(exclude_none=True)
                for name, entry in mcp_cfg.servers.items()
            }
            self.mcp_manager = MCPClientManager(config=servers_dict)
            await self.mcp_manager.initialize()

            # Main agent MCP server filter (config-driven)
            main_cfg = next(
                (a for a in self._app_config.agents if a.role == "main"),
                self._app_config.agents[0] if self._app_config.agents else None,
            )
            main_mcp_filter = main_cfg.mcp_filter if main_cfg else None
            if main_mcp_filter:
                mcp_tools = await _mcp_tools_for_agent(self.mcp_manager, main_mcp_filter)
                count = 0
                for tool in mcp_tools:
                    self.tool_manager.register(tool)
                    count += 1
                logger.info("Registered %d MCP tools for main", count)

        except ImportError as e:
            logger.warning("MCP adapter not available: %s", e)
        except Exception as e:
            logger.warning("MCP tools registration failed: %s", e)

    async def _register_multi_agent_tools(self) -> None:
        if self.tool_manager is None or self.subagent_service is None or self.broker is None:
            return

        agents = self._app_config.agents
        if len(agents) <= 1:
            return

        main_cfg = next((a for a in agents if a.role == "main"), agents[0] if agents else None)
        parent_name = main_cfg.name if main_cfg else "main"
        parent_address = AgentAddress(name=parent_name)

        strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)

        if self.agent_bus is not None:
            from framework.multi_agent.communication import AgentCommunicationService
            service = AgentCommunicationService(
                source=parent_address,
                broker=self.broker,
                registry=self.agent_pool,
                agent_bus=self.agent_bus,
                session_strategy=strategy,
                comm_tracker=self.communication_tracker,
            )
            self.tool_manager.register(SendToAgentAsyncTool(
                source=parent_address, broker=self.broker, registry=self.agent_pool,
                agent_bus=self.agent_bus, service=service,
                comm_tracker=self.communication_tracker,
            ))
            print("   [OK] send_to_agent_async registered")

            self.tool_manager.register(ListCommunicationTargetsTool(
                self_address=parent_address,
                registry=self.agent_pool,
            ))
            print("   [OK] list_communication_targets registered")

    # ── Peer / Subagent Tool Manager (code-driven) ──

    async def _build_subagent_tool_manager(
        self,
        tools: list[Tool],
        mcp_server_filter: list[str] | None = None,
    ) -> InMemoryToolManager:
        tm = InMemoryToolManager(config=ToolManagerConfig(
            max_workers=10, enable_parallel=True, parallel_max_workers=5,
        ))
        for tool in tools:
            tm.register(tool)

        if mcp_server_filter and self.mcp_manager:
            mcp_tools = await _mcp_tools_for_agent(self.mcp_manager, mcp_server_filter)
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

        project_dir = Path(__file__).parent.parent.parent
        sources: list[Any] = []

        default_dirs = [
            project_dir / "skills" / "subagents" / name,
            project_dir / "skills" / "peers" / name,
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
        builder = ProgressiveBuilder(base_path=project_dir)

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

        if isinstance(cfg, dict) and "pending_pruned_inputs" in cfg and "pending" not in cfg:
            cfg = {**cfg, "pending": cfg["pending_pruned_inputs"]}
        memory_cfg = cfg if isinstance(cfg, MemoryConfig) else MemoryConfig.model_validate(cfg)
        layer_config = _build_memory_layer_config(memory_cfg)
        if layer_config.pending is not None and not layer_config.pending.enabled:
            layer_config = replace(layer_config, pending=None)
        return layer_config

    def _session_only_memory_config(self, cfg: Any) -> MemoryLayerConfigSet:
        max_messages = 50
        if cfg is not None and hasattr(cfg, "short_term"):
            max_messages = cfg.short_term.max_messages
        elif isinstance(cfg, dict):
            short_term = cfg.get("short_term", {})
            max_messages = int(short_term.get("max_messages", max_messages))
        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=max_messages),
            archive=ArchiveMemoryConfig(scope=SessionScope()),
            knowledge=None,
            pending=PendingPrunedInputMemoryConfig(enabled=True),
        )

    async def _create_subagent_memory(self, sub_name: str, base_system_prompt: str = "") -> ContextManager:
        from framework.ioc.factories.compression import create_subagent_compression_coordinator
        from framework.memory.core.scope import MemoryAgentRole

        subagent_cfg = self._find_subagent_cfg()
        sub_memory_cfg = subagent_cfg.memory if subagent_cfg else None
        sub_dir = self._resolve_path("memory_dir", "data/memory") / "subagents" / sub_name
        sub_dir.mkdir(parents=True, exist_ok=True)
        coordinator = create_subagent_compression_coordinator(sub_memory_cfg)
        memory_system = create_memory_system(
            workspace=sub_dir, config=self._session_only_memory_config(sub_memory_cfg),
            session_only=False,
            lifecycle_policy=DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator),
        )
        await memory_system.initialize()
        if self.plugin_integration:
            self.plugin_integration.inject_memory_system_modifiers(memory_system)
        self._subagent_memory_systems[sub_name] = memory_system
        return MemorySystemContextManager(
            memory_system=memory_system, default_agent_id=sub_name,
            default_agent_role=MemoryAgentRole.SUBAGENT,
            base_system_prompt=base_system_prompt,
            injection_policy=RestrictedInjectionPolicy(max_session_messages=20),
        )

    async def _create_subagent_memory_context(self, peer_name: str, peer_cfg: Any) -> ContextManager:
        from framework.ioc.factories.compression import create_subagent_compression_coordinator
        from framework.memory.core.scope import MemoryAgentRole

        system_prompt = peer_cfg.system_prompt if peer_cfg else ""
        peer_memory_cfg = peer_cfg.memory if peer_cfg else None
        if peer_memory_cfg is None:
            from framework.core.context import InMemoryContextManager
            return InMemoryContextManager(base_system_prompt=system_prompt)

        peer_dir = self._resolve_path("memory_dir", "data/memory") / "peers" / peer_name
        peer_dir.mkdir(parents=True, exist_ok=True)

        coordinator = create_subagent_compression_coordinator(peer_memory_cfg)
        memory_system = create_memory_system(
            workspace=peer_dir, config=self._session_only_memory_config(peer_memory_cfg),
            session_only=False,
            lifecycle_policy=DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator),
        )
        await memory_system.initialize()
        if self.plugin_integration:
            self.plugin_integration.inject_memory_system_modifiers(memory_system)
        self._additional_subagent_memory_systems[peer_name] = memory_system
        max_messages = 50
        if peer_memory_cfg is not None and hasattr(peer_memory_cfg, "short_term"):
            max_messages = peer_memory_cfg.short_term.max_messages
        return MemorySystemContextManager(
            memory_system=memory_system, default_agent_id=peer_name,
            default_agent_role=MemoryAgentRole.SUBAGENT, base_system_prompt=system_prompt,
            injection_policy=RestrictedInjectionPolicy(max_session_messages=max_messages),
        )

    # ── Context Routing ──

    def _get_context_manager(self, session_id: str) -> ContextManager:
        if self.context_manager is None:
            raise RuntimeError("Context manager is not initialized")
        return self.context_manager

    # ── Cleanup ──

    async def _cleanup_subagent_memory(self, session_id: str) -> None:
        from framework.multi_agent.session_id import DefaultSessionIdStrategy
        main_cfg = self._main_agent_cfg
        parent_name = main_cfg.name if main_cfg else "main"
        strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
        _, sub_name = strategy.parse(session_id)
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

    # ── Peer Agent Initialization ──

    async def _initialize_additional_subagents(self) -> None:
        from framework.hook import HookErrorPolicy, HookSpec
        from framework.hook.builtin import SubagentAutoSendHook
        from framework.ioc.factories.descriptors import build_peer_descriptor

        if self.agent_pool is None or self.broker is None or self.subagent_service is None:
            return

        peer_cfgs = self._find_additional_subagent_cfgs()
        main_cfg = self._main_agent_cfg
        parent_name = main_cfg.name if main_cfg else "main"

        if not peer_cfgs:
            return
        if self.mode != "pool":
            print(f"   [WARN] {len(peer_cfgs)} peer agents require pool mode")
            return

        print(f"\n[INIT] Initializing {len(peer_cfgs)} peer agents...")
        memory_dir = self._resolve_path("memory_dir", "data/memory")
        for peer_cfg in peer_cfgs:
            peer_name = peer_cfg.name
            print(f"[INIT] Initializing peer agent: {peer_name}")

            descriptor, tool_manager, skill_manager, memory_ctx = await build_peer_descriptor(
                peer_cfg, self._app_config, self._project_dir,
                memory_dir, self.safety_policy, self.provider,
            )

            # Per-agent MCP tool injection (config-driven, no name checks)
            if peer_cfg.mcp_filter and self.mcp_manager:
                mcp_tools = await _mcp_tools_for_agent(self.mcp_manager, peer_cfg.mcp_filter)
                for tool in mcp_tools:
                    tool_manager.register(tool)

            from framework.multi_agent.peer_validator import SubagentAgentValidator
            SubagentAgentValidator.validate(descriptor, parent_name)

            context_manager = memory_ctx

            # register send_to_agent_async (star topology)
            peer_address = AgentAddress(name=peer_name)
            strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
            from framework.multi_agent.communication import AgentCommunicationService
            peer_service = AgentCommunicationService(
                source=peer_address, broker=self.broker, registry=self.agent_pool,
                agent_bus=self.agent_bus, session_strategy=strategy,
                comm_tracker=self.communication_tracker,
            )
            tool_manager.register(SendToAgentAsyncTool(
                source=peer_address, broker=self.broker, registry=self.agent_pool,
                agent_bus=self.agent_bus, service=peer_service,
                comm_tracker=self.communication_tracker,
            ))
            tool_manager.register(ListCommunicationTargetsTool(
                self_address=peer_address,
                registry=self.agent_pool,
            ))

            await self.agent_pool.register_resident(
                descriptor, context_manager=context_manager, tool_manager=tool_manager,
                skill_manager=skill_manager, output_adapter=NullOutputAdapter(),
            )

            instance = self.agent_pool.get(peer_name)
            if instance and instance.pipeline:
                instance.pipeline.governance = create_subagent_governance(
                    peer_cfg.memory, self._app_config.llm.max_tokens,
                )
                instance.pipeline.context_manager_factory = None

            if self.agent_bus is not None and instance and instance.pipeline:
                hook = SubagentAutoSendHook(agent_bus=self.agent_bus, self_name=peer_name, parent_name=parent_name)
                if instance.pipeline.hook_runner is not None:
                    instance.pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
                else:
                    instance.pipeline.hooks.append(hook)

            print(f"[OK] Peer agent '{peer_name}' registered as resident")
        print(f"[OK] {len(peer_cfgs)} peer agents initialized\n")
