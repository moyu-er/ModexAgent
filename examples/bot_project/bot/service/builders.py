"""Agent builder mixin for BotService — tool registration, memory, descriptors.

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

import logging
from pathlib import Path
from typing import Any

from bot.tools.custom import SpawnSubagentTool
from framework import InMemoryToolManager, ToolManagerConfig
from framework.core.context import ContextManager
from framework.core.skills import (
    CompositeSkillSource,
    FileSkillSource,
    ProgressiveBuilder,
    SkillManager,
)
from framework.core.tool_manager import Tool
from framework.memory.core.scope import MemoryAgentRole, MemoryContext
from framework.memory.injection import RestrictedInjectionPolicy
from framework.memory.layers.config import (
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
)
from framework.memory.system import MemorySystemContextManager, create_memory_system
from framework.multi_agent import AgentAddress, AgentDescriptor
from framework.multi_agent.descriptor import AgentLLMConfig
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.tools import SendMessageAsyncTool, SendMessageTool
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

    adapter = MCPToolAdapter(mcp_manager=mcp_manager, default_prefix=True)
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

            mcp_config = self.config.get("mcp", {})
            servers = mcp_config.get("servers", {})
            if not servers:
                return

            logger.info("Connecting to %d MCP servers...", len(servers))
            self.mcp_manager = MCPClientManager(config=servers)
            await self.mcp_manager.initialize()

            # Main agent MCP server filter
            main_mcp_filter = mcp_config.get("server_filter", ["fetch", "mcp-deepwiki", "MiniMax", "playwright"])
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
        if self.tool_manager is None or self.subagent_manager is None or self.broker is None:
            return

        multi_agent_config = self.config.get("multi_agent", {})
        if not multi_agent_config.get("enabled", True):
            return

        parent_name = multi_agent_config.get("parent_agent_name", "main")
        parent_address = AgentAddress(name=parent_name)
        peers_config = multi_agent_config.get("peers", [])
        peer_names = [p.get("name", "") for p in peers_config if p.get("name")]

        strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
        self.tool_manager.register(SendMessageTool(
            broker=self.broker, self_address=parent_address,
            allowed_targets=peer_names or None, registry=self.agent_pool,
            session_strategy=strategy,
        ))
        print("   [OK] send_message registered")

        if self.agent_bus is not None:
            self.tool_manager.register(SendMessageAsyncTool(
                broker=self.broker, self_address=parent_address,
                allowed_targets=peer_names or None, agent_bus=self.agent_bus,
                registry=self.agent_pool, session_strategy=strategy,
            ))
            print("   [OK] send_message_async registered")

        sub_sync = multi_agent_config.get("subagent_sync", {})
        if sub_sync.get("enabled", True):
            descriptor, tool_manager, skill_manager = await self._build_subagent_descriptor(sub_sync)
            self.tool_manager.register(SpawnSubagentTool(
                manager=self.subagent_manager, default_parent_address=parent_address,
                descriptor=descriptor, tool_manager=tool_manager,
                skill_manager=skill_manager, broker=self.broker,
                agent_bus=self.agent_bus, registry=self.agent_pool,
            ))
            print(f"   [OK] spawn_subagent_sync registered")

    # ── Peer / Subagent Tool Manager (code-driven) ──

    async def _build_peer_tool_manager(
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
        mgr = SkillManager(source=source, builder=builder)
        self._subagent_skill_managers[cache_key] = mgr
        return mgr

    # ── Memory Creation ──

    def _session_only_memory_config(self, section: dict[str, Any]) -> MemoryLayerConfigSet:
        short_term = section.get("short_term", {})
        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=short_term.get("max_messages", 50)),
            archive=None, knowledge=None,
            pending=PendingPrunedInputMemoryConfig(enabled=True),
        )

    def _merge_peer_memory_config(self, peer_config: dict[str, Any]) -> dict[str, Any]:
        memory_config = peer_config.get("memory", {})
        global_peer_memory = self.config.get("memory", {}).get("peers", {})
        merged = {**global_peer_memory, **{k: v for k, v in memory_config.items() if k != "enabled"}}
        for section_name in ("short_term", "governance"):
            if section_name in global_peer_memory or section_name in memory_config:
                merged[section_name] = {
                    **global_peer_memory.get(section_name, {}),
                    **memory_config.get(section_name, {}),
                }
        return merged

    def _build_peer_compression_coordinator(self, memory_section: dict[str, Any]) -> Any:
        from framework.memory.compression.policies import DefaultMemoryCompressionCoordinator
        short_term = memory_section.get("short_term", {})
        return DefaultMemoryCompressionCoordinator(
            max_messages=short_term.get("max_messages", 50),
            max_tokens=short_term.get("max_tokens", 8000),
            keep_ratio_for_messages=short_term.get("keep_ratio_for_messages", 0.5),
            keep_ratio_for_token=short_term.get("keep_ratio_for_token", 0.5),
        )

    def _build_peer_governance(self, memory_section: dict[str, Any] | None = None) -> Any | None:
        from framework.memory.context_governance import (
            CompositeGovernance, FinalContextLegalityGovernance,
            PriorityBudgetGovernance, ToolChainRepairGovernance,
        )
        from framework.memory.retention import DefaultMessageRetentionPolicy

        gov_config = (memory_section or {}).get("governance", {})
        if not gov_config.get("enabled", True):
            return None

        strategies: list[Any] = [ToolChainRepairGovernance()]
        token_budget = gov_config.get("token_budget", {})
        if token_budget.get("enabled", True):
            llm_max_tokens = self.config.get("llm", {}).get("max_tokens", 80000)
            max_tokens = min(int(llm_max_tokens * token_budget.get("budget_ratio", 0.3)), 64000)
            strategies.append(PriorityBudgetGovernance(
                max_tokens=max_tokens, safety_buffer=token_budget.get("safety_buffer", 512),
                retention_policy=DefaultMessageRetentionPolicy(),
            ))
        strategies.append(FinalContextLegalityGovernance())
        return CompositeGovernance(strategies)

    async def _create_subagent_memory(self, sub_name: str, base_system_prompt: str = "") -> ContextManager:
        from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy
        memory_config = self.config.get("memory", {}).get("subagents", {})
        sub_dir = self._resolve_path("memory_dir", "data/memory") / "subagents" / sub_name
        sub_dir.mkdir(parents=True, exist_ok=True)
        coordinator = self._build_peer_compression_coordinator(memory_config)
        memory_system = create_memory_system(
            workspace=sub_dir, config=self._session_only_memory_config(memory_config),
            session_only=True,
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

    async def _create_peer_memory(self, peer_name: str, peer_config: dict[str, Any]) -> ContextManager:
        system_prompt = peer_config.get("system_prompt", "")
        if not peer_config.get("memory", {}).get("enabled", True):
            from framework.core.context import InMemoryContextManager
            return InMemoryContextManager(base_system_prompt=system_prompt)

        merged = self._merge_peer_memory_config(peer_config)
        peer_dir = self._resolve_path("memory_dir", "data/memory") / "peers" / peer_name
        peer_dir.mkdir(parents=True, exist_ok=True)
        from framework.memory.lifecycle import DefaultMemoryLifecyclePolicy

        coordinator = self._build_peer_compression_coordinator(merged)
        memory_system = create_memory_system(
            workspace=peer_dir, config=self._session_only_memory_config(merged),
            session_only=True,
            lifecycle_policy=DefaultMemoryLifecyclePolicy(compression_coordinator=coordinator),
        )
        await memory_system.initialize()
        if self.plugin_integration:
            self.plugin_integration.inject_memory_system_modifiers(memory_system)
        self._peer_memory_systems[peer_name] = memory_system
        return MemorySystemContextManager(
            memory_system=memory_system, default_agent_id=peer_name,
            default_agent_role=MemoryAgentRole.PEER, base_system_prompt=system_prompt,
            injection_policy=RestrictedInjectionPolicy(
                max_session_messages=merged.get("short_term", {}).get("max_messages", 50),
            ),
        )

    # ── Descriptor Building ──

    async def _build_subagent_descriptor(
        self, sub_config: dict[str, Any], skill_manager: SkillManager | None = None,
    ) -> tuple[AgentDescriptor, Any | None, Any | None]:
        llm = self.config.get("llm", {})
        agent = self.config.get("agent", {})
        sub_name = sub_config.get("name", "helper")

        # Subagent gets standard tools (no MCP)
        sub_tools = list(_make_standard_tools())
        tool_manager = await self._build_peer_tool_manager(sub_tools, mcp_server_filter=None)

        skill_dirs = sub_config.get("skill_dirs", [])
        if skill_manager is None and (sub_config.get("allowed_skills") is not None or skill_dirs):
            project_dir = Path(__file__).parent.parent.parent
            extra_dirs = [project_dir / d for d in skill_dirs] if skill_dirs else None
            skill_manager = self._get_subagent_skill_manager(name=sub_name, extra_dirs=extra_dirs)

        context_manager = await self._create_subagent_memory(
            sub_name, base_system_prompt=sub_config.get("system_prompt", agent.get("system_prompt", "")))

        allowed_skills: list[str] | None = sub_config.get("allowed_skills")
        if allowed_skills is None and not skill_dirs:
            allowed_skills = []

        safety = getattr(self, "safety_policy", None)
        descriptor = AgentDescriptor(
            address=AgentAddress(name=sub_name, role=sub_config.get("role"),
                                  capabilities=sub_config.get("capabilities", [])),
            llm_config=AgentLLMConfig(model=llm.get("model"),
                temperature=llm.get("temperature", 0.7), max_tokens=llm.get("max_tokens", 2000)),
            system_prompt_template=sub_config.get("system_prompt", agent.get("system_prompt", "")),
            allowed_tools=sub_config.get("allowed_tools"),
            denied_tools=sub_config.get("denied_tools"),
            allowed_skills=allowed_skills,
            max_iterations=sub_config.get("max_iterations", 15),
            max_tools_per_turn=sub_config.get("max_tools_per_turn", 10),
            execution_strategy="react", context_manager=context_manager,
            context_strategy=sub_config.get("context_strategy", "ephemeral"),
            streaming_to_user=False, internal_streaming=False, safety_policy=safety,
        )
        return descriptor, tool_manager, skill_manager

    async def _build_peer_descriptor(
        self, peer_config: dict[str, Any],
    ) -> tuple[AgentDescriptor, InMemoryToolManager, SkillManager | None]:
        llm = self.config.get("llm", {})
        agent = self.config.get("agent", {})
        peer_name = peer_config.get("name", "unnamed")

        # Peer tools: determined by code, not config
        if peer_name == "query-12306":
            # query-12306: MCP-only, no standard tools
            peer_tools: list[Tool] = []
            mcp_filter: list[str] | None = ["12306-mcp"]
        elif peer_name == "office-expert":
            peer_tools = [*_make_file_tools(), _make_shell_tool(), *[]]  # file + shell, no search
            mcp_filter = None
        else:
            peer_tools = list(_make_standard_tools())
            mcp_filter = None

        tool_manager = await self._build_peer_tool_manager(peer_tools, mcp_server_filter=mcp_filter)

        skill_dirs = peer_config.get("skill_dirs", [])
        allowed_skills = peer_config.get("allowed_skills")
        if allowed_skills is not None or skill_dirs:
            project_dir = Path(__file__).parent.parent.parent
            extra_dirs = [project_dir / d for d in skill_dirs] if skill_dirs else None
            skill_manager = self._get_subagent_skill_manager(name=peer_name, extra_dirs=extra_dirs)
        else:
            skill_manager = None

        safety = getattr(self, "safety_policy", None)
        descriptor = AgentDescriptor(
            address=AgentAddress(name=peer_name, role=peer_config.get("role"),
                                  capabilities=peer_config.get("capabilities", [])),
            llm_config=AgentLLMConfig(model=llm.get("model"),
                temperature=llm.get("temperature", 0.7), max_tokens=llm.get("max_tokens", 2000)),
            system_prompt_template=peer_config.get("system_prompt", agent.get("system_prompt", "")),
            allowed_tools=peer_config.get("allowed_tools"),
            denied_tools=peer_config.get("denied_tools"),
            allowed_skills=peer_config.get("allowed_skills"),
            max_iterations=peer_config.get("max_iterations", 15),
            max_tools_per_turn=peer_config.get("max_tools_per_turn", 10),
            execution_strategy="react",
            context_strategy=peer_config.get("context_strategy", "persistent"),
            safety_policy=safety,
        )
        return descriptor, tool_manager, skill_manager

    # ── Context Routing ──

    def _get_context_manager(self, session_id: str) -> ContextManager:
        if self.context_manager is None:
            raise RuntimeError("Context manager is not initialized")
        return self.context_manager

    # ── Cleanup ──

    async def _cleanup_subagent_memory(self, session_id: str) -> None:
        from framework.multi_agent.session_id import DefaultSessionIdStrategy
        parent_name = self.config.get("multi_agent", {}).get("parent_agent_name", "main")
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

    async def _initialize_peer_agents(self) -> None:
        if self.agent_pool is None or self.broker is None or self.subagent_manager is None:
            return

        multi_agent_config = self.config.get("multi_agent", {})
        peers_config = multi_agent_config.get("peers", [])
        parent_name = multi_agent_config.get("parent_agent_name", "main")

        if not peers_config:
            return
        if self.mode != "pool":
            print(f"   [WARN] {len(peers_config)} peer agents require pool mode")
            return

        print(f"\n[INIT] Initializing {len(peers_config)} peer agents...")
        for peer_config in peers_config:
            peer_name = peer_config.get("name", "unnamed")
            print(f"[INIT] Initializing peer agent: {peer_name}")

            descriptor, tool_manager, skill_manager = await self._build_peer_descriptor(peer_config)

            from framework.multi_agent.peer_validator import PeerAgentValidator
            PeerAgentValidator.validate(descriptor, parent_name)

            context_manager = await self._create_peer_memory(peer_name, peer_config)

            # register send_message_async (star topology)
            peer_address = AgentAddress(name=peer_name)
            strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
            tool_manager.register(SendMessageAsyncTool(
                broker=self.broker, self_address=peer_address, allowed_targets=[parent_name],
                agent_bus=self.agent_bus, registry=self.agent_pool,
                session_strategy=strategy,
            ))

            # register subagent tool for this peer
            if peer_config.get("subagent", {}).get("enabled", False):
                sub_sync = multi_agent_config.get("subagent_sync", {})
                if sub_sync.get("enabled", True):
                    sub_descriptor, sub_tm, sub_sm = await self._build_subagent_descriptor(sub_sync)
                    tool_manager.register(SpawnSubagentTool(
                        manager=self.subagent_manager,
                        default_parent_address=peer_address,
                        descriptor=sub_descriptor, tool_manager=sub_tm,
                        skill_manager=sub_sm, broker=self.broker,
                        agent_bus=self.agent_bus, registry=self.agent_pool,
                    ))

            await self.agent_pool.register_resident(
                descriptor, context_manager=context_manager, tool_manager=tool_manager,
                skill_manager=skill_manager, output_adapter=NullOutputAdapter(),
            )

            instance = self.agent_pool.get(peer_name)
            if instance and instance.pipeline:
                merged = self._merge_peer_memory_config(peer_config)
                instance.pipeline.governance = self._build_peer_governance(merged)
                instance.pipeline.context_manager_factory = None

            if self.agent_bus is not None and instance and instance.pipeline:
                from framework.hook import HookErrorPolicy, HookSpec
                from framework.hook.builtin import PeerAutoSendHook

                hook = PeerAutoSendHook(agent_bus=self.agent_bus, self_name=peer_name, parent_name=parent_name)
                if instance.pipeline.hook_runner is not None:
                    instance.pipeline.hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
                else:
                    instance.pipeline.hooks.append(hook)

            print(f"[OK] Peer agent '{peer_name}' registered as resident")
        print(f"[OK] {len(peers_config)} peer agents initialized\n")
