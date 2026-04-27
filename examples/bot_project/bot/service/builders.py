"""Agent builder mixin for BotService.

Contains all methods related to building, registering, and configuring
peer agents, subagents, tools, skills, and memory systems.
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
from framework.memory.core.scope import MemoryAgentRole, MemoryContext
from framework.memory.injection import RestrictedInjectionPolicy
from framework.memory.layers.config import MemoryLayerConfigSet, SessionMemoryConfig
from framework.memory.system import MemorySystemContextManager, create_memory_system
from framework.multi_agent import (
    AgentAddress,
    AgentDescriptor,
)
from framework.multi_agent.descriptor import AgentLLMConfig
from framework.multi_agent.session_id import DefaultSessionIdStrategy
from framework.multi_agent.tools import SendMessageAsyncTool, SendMessageTool
from framework.pipeline.adapters import NullOutputAdapter

logger = logging.getLogger(__name__)


class AgentBuilderMixin:
    """Mixin providing tool registration, skill/memory management, and agent building.

    Expects the host class to have these attributes:
        config, provider, tool_manager, mcp_manager, output_adapter,
        broker, agent_bus, agent_pool, subagent_manager, plugin_integration,
        _subagent_skill_managers, _peers_skill_manager, _subagent_memory_systems,
        _peer_memory_systems, inbox_server, agent_factory
    """

    # --- Tool Registration ---

    async def _register_tools(self) -> None:
        if self.tool_manager is None:
            logger.warning("ToolManager not initialized, skipping tool registration")
            return


        from framework.tools.standard import (
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )

        tools_config = self.config.get("tools", {})
        project_dir = Path(__file__).parent.parent.parent

        file_tools_config = tools_config.get("file_tools", {})
        if file_tools_config.get("enabled", True):
            allowed_dirs = file_tools_config.get("allowed_directories", ["."])
            resolved_dirs = [project_dir / d for d in allowed_dirs]
            allowed_dir = resolved_dirs[0] if len(resolved_dirs) == 1 else project_dir.resolve()

            self.tool_manager.register(ReadFileTool(allowed_dir=allowed_dir))
            self.tool_manager.register(WriteFileTool(allowed_dir=allowed_dir))
            self.tool_manager.register(EditFileTool(allowed_dir=allowed_dir))
            self.tool_manager.register(ListDirTool(allowed_dir=allowed_dir))
            print("   [OK] File tools registered (read_file, write_file, edit_file, list_dir)")
            print(f"      Allowed dir: {allowed_dir}")

        shell_tools_config = tools_config.get("shell_tools", {})
        if shell_tools_config.get("enabled", True):
            from framework.tools.standard import ShellTool

            working_dir = shell_tools_config.get("working_dir")
            if working_dir:
                working_dir = str((project_dir / working_dir).resolve())
            else:
                working_dir = str(project_dir.resolve())

            shell_tool = ShellTool(
                timeout=shell_tools_config.get("timeout", 60),
                working_dir=working_dir,
                enable_safety_guard=shell_tools_config.get("enable_safety_guard", True),
                restrict_to_workspace=shell_tools_config.get("restrict_to_workspace", True),
            )
            self.tool_manager.register(shell_tool)
            print("   [OK] Shell tool registered (shell)")
            print(f"      Working dir: {working_dir}")

        from bot.tools.custom import SendFileToUserTool as _SendFileToUserTool

        self.tool_manager.register(_SendFileToUserTool(output_adapter=self.output_adapter))
        print("   [OK] Send file tool registered (send_file_to_user)")

    async def _register_mcp_tools(self) -> None:
        if self.tool_manager is None:
            logger.warning("ToolManager not initialized, skipping MCP tools registration")
            return

        try:
            from framework.tools.mcp import MCPClientManager
            from framework.tools.mcp_adapter import MCPToolAdapter

            mcp_config = self.config.get("mcp", {})
            servers = mcp_config.get("servers", {})
            if not servers:
                logger.warning("No MCP server configuration")
                return

            logger.info("Connecting to %d MCP servers...", len(servers))
            self.mcp_manager = MCPClientManager(config=servers)
            await self.mcp_manager.initialize()

            tools_config = self.config.get("tools", {})
            mcp_tools_config = tools_config.get("mcp_tools", {})
            tool_timeout = mcp_tools_config.get("tool_timeout", 30)
            server_filter = mcp_tools_config.get("server_filter")

            adapter = MCPToolAdapter(
                mcp_manager=self.mcp_manager,
                default_prefix=True,
                tool_timeout=tool_timeout,
            )
            registered = await adapter.register_tools(
                registry=self.tool_manager,
                server_filter=server_filter,
                tool_filter=mcp_tools_config.get("tool_filter"),
            )
            logger.info("Registered %d MCP tools: %s", len(registered), registered)

        except ImportError as e:
            logger.warning("MCP adapter not available: %s", e)
        except Exception as e:
            logger.warning("MCP tools registration failed: %s", e)

    async def _register_multi_agent_tools(self) -> None:
        if self.tool_manager is None or self.subagent_manager is None or self.broker is None:
            return

        multi_agent_config = self.config.get("multi_agent", {})
        if not multi_agent_config.get("enabled", True):
            print("   [INFO] multi_agent disabled, skipping multi-agent tool registration")
            return

        parent_address = AgentAddress(name=multi_agent_config.get("parent_agent_name", "main"))
        peers_config = multi_agent_config.get("peers", [])
        peer_names = [p.get("name", "") for p in peers_config if p.get("name")]

        allowed_callers = multi_agent_config.get("allowed_callers")
        parent_name = multi_agent_config.get("parent_agent_name", "main")
        strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
        send_tool = SendMessageTool(
            broker=self.broker,
            self_address=parent_address,
            allowed_callers=allowed_callers,
            allowed_targets=peer_names or None,
            registry=self.agent_pool,
            session_strategy=strategy,
        )
        self.tool_manager.register(send_tool)
        print("   [OK] send_message registered")

        if self.agent_bus is not None:
            async_send_tool = SendMessageAsyncTool(
                broker=self.broker,
                self_address=parent_address,
                allowed_callers=allowed_callers,
                allowed_targets=peer_names or None,
                agent_bus=self.agent_bus,
                registry=self.agent_pool,
                wakeup_timeout=5.0,
                session_strategy=strategy,
            )
            self.tool_manager.register(async_send_tool)
            print("   [OK] send_message_async registered")

        sub_sync = multi_agent_config.get("subagent_sync", {})
        if sub_sync.get("enabled", True):
            descriptor, tool_manager, skill_manager = await self._build_subagent_descriptor(sub_sync)
            spawn_tool = SpawnSubagentTool(
                manager=self.subagent_manager,
                default_parent_address=parent_address,
                descriptor=descriptor,
                tool_manager=tool_manager,
                skill_manager=skill_manager,
                broker=self.broker,
                agent_bus=self.agent_bus,
                registry=self.agent_pool,
            )
            self.tool_manager.register(spawn_tool)
            print(f"   [OK] spawn_subagent_sync registered (subagent={descriptor.address.name})")

    # --- Peer / Subagent Tool Manager ---

    async def _build_peer_tool_manager(
        self,
        tools_config: dict[str, Any],
        mcp_server_filter: list[str] | None = None,
        peer_name: str | None = None,
    ) -> InMemoryToolManager:
        tm_config = ToolManagerConfig(max_workers=10, enable_parallel=True, parallel_max_workers=5)
        tm = InMemoryToolManager(config=tm_config)
        project_dir = Path(__file__).parent.parent.parent

        extra_allowed_dirs: list[Path] = []
        if peer_name:
            for skills_base in ["skills/subagents", "skills/peers"]:
                skill_dir = project_dir / skills_base / peer_name
                if skill_dir.exists():
                    extra_allowed_dirs.append(skill_dir.resolve())

        from framework.tools.standard import (
            EditFileTool,
            ListDirTool,
            ReadFileTool,
            WriteFileTool,
        )

        file_tools_config = tools_config.get("file_tools", {})
        if file_tools_config.get("enabled", True):
            allowed_dirs = file_tools_config.get("allowed_directories", ["."])
            resolved_dirs = [project_dir / d for d in allowed_dirs]
            allowed_dir = resolved_dirs[0] if len(resolved_dirs) == 1 else project_dir.resolve()
            extra = extra_allowed_dirs or None
            tm.register(ReadFileTool(allowed_dir=allowed_dir, extra_allowed_dirs=extra))
            tm.register(WriteFileTool(allowed_dir=allowed_dir, extra_allowed_dirs=extra))
            tm.register(EditFileTool(allowed_dir=allowed_dir, extra_allowed_dirs=extra))
            tm.register(ListDirTool(allowed_dir=allowed_dir, extra_allowed_dirs=extra))

        from framework.tools.standard import ShellTool

        shell_tools_config = tools_config.get("shell_tools", {})
        if shell_tools_config.get("enabled", True):
            working_dir = shell_tools_config.get("working_dir")
            if working_dir:
                working_dir = str((project_dir / working_dir).resolve())
            else:
                working_dir = str(project_dir.resolve())
            tm.register(ShellTool(
                timeout=shell_tools_config.get("timeout", 60),
                working_dir=working_dir,
                enable_safety_guard=shell_tools_config.get("enable_safety_guard", True),
                restrict_to_workspace=shell_tools_config.get("restrict_to_workspace", True),
            ))

        mcp_tools_config = tools_config.get("mcp_tools", {})
        if mcp_tools_config.get("enabled", False) and mcp_server_filter and self.mcp_manager:
            from framework.tools.mcp_adapter import MCPToolAdapter

            adapter = MCPToolAdapter(
                mcp_manager=self.mcp_manager,
                default_prefix=True,
                tool_timeout=mcp_tools_config.get("tool_timeout", 30),
            )
            try:
                registered = await adapter.register_tools(
                    registry=tm,
                    server_filter=mcp_server_filter,
                    tool_filter=mcp_tools_config.get("tool_filter"),
                )
                logger.debug("[%s] Registered MCP tools: %s", peer_name, registered)
            except Exception as e:
                logger.warning("[%s] Failed to register MCP tools: %s", peer_name, e)

        return tm

    # --- Skill Management ---

    def _get_subagent_skill_manager(
        self,
        name: str,
        extra_dirs: list[Path] | None = None,
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
                directories=found_default, cache=True, layout="directory", skill_filename="SKILL.md",
            ))

        if extra_dirs:
            found_extra = [d for d in extra_dirs if d.exists()]
            if found_extra:
                sources.append(FileSkillSource(
                    directories=found_extra, cache=True, layout="flat", skill_filename="SKILL.md",
                ))

        if not sources:
            return None

        source = (
            CompositeSkillSource(sources=sources, merge_strategy="last_wins")
            if len(sources) > 1
            else sources[0]
        )

        builder = ProgressiveBuilder(base_path=project_dir)
        mgr = SkillManager(source=source, builder=builder)
        self._subagent_skill_managers[cache_key] = mgr
        return mgr

    # --- Memory Creation ---

    def _session_only_memory_config(self, section: dict[str, Any]) -> MemoryLayerConfigSet:
        short_term = section.get("short_term", {})
        return MemoryLayerConfigSet(
            session=SessionMemoryConfig(max_messages=short_term.get("max_messages", 50)),
            archive=None,
            knowledge=None,
        )

    async def _create_subagent_memory(
        self,
        sub_name: str,
        base_system_prompt: str = "",
    ) -> ContextManager:
        memory_config = self.config.get("memory", {}).get("subagents", {})
        sub_dir = self._resolve_path("memory_dir", "data/memory") / "subagents" / sub_name
        sub_dir.mkdir(parents=True, exist_ok=True)
        memory_system = create_memory_system(
            workspace=sub_dir,
            config=self._session_only_memory_config(memory_config),
            session_only=True,
        )
        await memory_system.initialize()
        _pi = getattr(self, "plugin_integration", None)
        if _pi is not None:
            _pi.inject_memory_system_modifiers(memory_system)
        self._subagent_memory_systems[sub_name] = memory_system
        return MemorySystemContextManager(
            memory_system=memory_system,
            default_agent_id=sub_name,
            default_agent_role=MemoryAgentRole.SUBAGENT,
            base_system_prompt=base_system_prompt,
            injection_policy=RestrictedInjectionPolicy(max_session_messages=20),
        )

    async def _create_peer_memory(
        self,
        peer_name: str,
        peer_config: dict[str, Any],
    ) -> ContextManager:
        system_prompt = peer_config.get("system_prompt", "")
        memory_config = peer_config.get("memory", {})
        if not memory_config.get("enabled", True):
            from framework.core.context import InMemoryContextManager
            return InMemoryContextManager(base_system_prompt=system_prompt)

        global_peer_memory = self.config.get("memory", {}).get("peers", {})
        merged_memory = {
            **global_peer_memory,
            **{key: value for key, value in memory_config.items() if key != "enabled"},
        }
        if "short_term" in global_peer_memory or "short_term" in memory_config:
            merged_memory["short_term"] = {
                **global_peer_memory.get("short_term", {}),
                **memory_config.get("short_term", {}),
            }

        peer_dir = self._resolve_path("memory_dir", "data/memory") / "peers" / peer_name
        peer_dir.mkdir(parents=True, exist_ok=True)
        memory_system = create_memory_system(
            workspace=peer_dir,
            config=self._session_only_memory_config(merged_memory),
            session_only=True,
        )
        await memory_system.initialize()
        _pi = getattr(self, "plugin_integration", None)
        if _pi is not None:
            _pi.inject_memory_system_modifiers(memory_system)
        if not hasattr(self, "_peer_memory_systems"):
            self._peer_memory_systems: dict[str, Any] = {}
        self._peer_memory_systems[peer_name] = memory_system
        return MemorySystemContextManager(
            memory_system=memory_system,
            default_agent_id=peer_name,
            default_agent_role=MemoryAgentRole.PEER,
            base_system_prompt=system_prompt,
            injection_policy=RestrictedInjectionPolicy(
                max_session_messages=merged_memory.get("short_term", {}).get("max_messages", 50),
            ),
        )

    # --- Descriptor Building ---

    async def _build_subagent_descriptor(
        self,
        sub_config: dict[str, Any],
        skill_manager: SkillManager | None = None,
    ) -> tuple[AgentDescriptor, Any | None, Any | None]:
        llm = self.config.get("llm", {})
        agent = self.config.get("agent", {})
        tools_config = sub_config.get("tools")
        tool_manager = await self._build_peer_tool_manager(tools_config) if tools_config else None

        has_skill_dirs = bool(sub_config.get("skill_dirs"))
        has_allowed_skills = sub_config.get("allowed_skills") is not None
        if skill_manager is None and (has_allowed_skills or has_skill_dirs):
            project_dir = Path(__file__).parent.parent.parent
            extra_dirs = [project_dir / d for d in sub_config.get("skill_dirs", [])]
            skill_manager = self._get_subagent_skill_manager(
                name=sub_config.get("name", "helper"),
                extra_dirs=extra_dirs or None,
            )

        sub_name = sub_config.get("name", "helper")
        context_manager = await self._create_subagent_memory(
            sub_name,
            base_system_prompt=sub_config.get("system_prompt", agent.get("system_prompt", "")),
        )

        if not has_allowed_skills and not has_skill_dirs:
            allowed_skills: list[str] | None = []
        else:
            allowed_skills = sub_config.get("allowed_skills")

        safety = getattr(self, "safety_policy", None) or self._build_safety_policy()
        descriptor = AgentDescriptor(
            address=AgentAddress(
                name=sub_name,
                role=sub_config.get("role"),
                capabilities=sub_config.get("capabilities", []),
            ),
            llm_config=AgentLLMConfig(
                model=llm.get("model"),
                temperature=llm.get("temperature", 0.7),
                max_tokens=llm.get("max_tokens", 2000),
            ),
            system_prompt_template=sub_config.get("system_prompt", agent.get("system_prompt", "")),
            allowed_tools=sub_config.get("allowed_tools"),
            denied_tools=sub_config.get("denied_tools"),
            allowed_skills=allowed_skills,
            max_iterations=sub_config.get("max_iterations", agent.get("max_iterations", 15)),
            max_tools_per_turn=sub_config.get("max_tools_per_turn", 10),
            execution_strategy=sub_config.get("execution_strategy", "react"),
            context_manager=context_manager,
            context_strategy=sub_config.get("context_strategy", "ephemeral"),
            streaming_to_user=False,
            internal_streaming=False,
            safety_policy=safety,
        )
        return descriptor, tool_manager, skill_manager

    async def _build_peer_descriptor(
        self,
        peer_config: dict[str, Any],
    ) -> tuple[AgentDescriptor, InMemoryToolManager, SkillManager | None]:
        llm = self.config.get("llm", {})
        agent = self.config.get("agent", {})
        tools_config = peer_config.get("tools")
        mcp_tools_config = tools_config.get("mcp_tools", {}) if tools_config else {}
        mcp_server_filter = (
            mcp_tools_config.get("server_filter")
            if mcp_tools_config.get("enabled", False)
            else None
        )
        peer_name = peer_config.get("name", "unnamed")
        tool_manager = (
            await self._build_peer_tool_manager(
                tools_config,
                mcp_server_filter=mcp_server_filter,
                peer_name=peer_name,
            )
            if tools_config
            else InMemoryToolManager(config=ToolManagerConfig())
        )

        has_skill_dirs = bool(peer_config.get("skill_dirs"))
        has_allowed_skills = peer_config.get("allowed_skills") is not None
        if has_allowed_skills or has_skill_dirs:
            project_dir = Path(__file__).parent.parent.parent
            extra_dirs = [project_dir / d for d in peer_config.get("skill_dirs", [])]
            skill_manager = self._get_subagent_skill_manager(
                name=peer_name,
                extra_dirs=extra_dirs or None,
            )
        else:
            skill_manager = None

        safety = getattr(self, "safety_policy", None) or self._build_safety_policy()
        descriptor = AgentDescriptor(
            address=AgentAddress(
                name=peer_name,
                role=peer_config.get("role"),
                capabilities=peer_config.get("capabilities", []),
            ),
            llm_config=AgentLLMConfig(
                model=llm.get("model"),
                temperature=llm.get("temperature", 0.7),
                max_tokens=llm.get("max_tokens", 2000),
            ),
            system_prompt_template=peer_config.get("system_prompt", agent.get("system_prompt", "")),
            allowed_tools=peer_config.get("allowed_tools"),
            denied_tools=peer_config.get("denied_tools"),
            allowed_skills=peer_config.get("allowed_skills"),
            max_iterations=peer_config.get("max_iterations", agent.get("max_iterations", 15)),
            max_tools_per_turn=peer_config.get("max_tools_per_turn", 10),
            execution_strategy=peer_config.get("execution_strategy", "react"),
            context_strategy=peer_config.get("context_strategy", "persistent"),
            safety_policy=safety,
        )
        return descriptor, tool_manager, skill_manager

    # --- Context Routing ---

    def _get_context_manager(self, session_id: str) -> ContextManager:
        """Return the appropriate context manager for a session.

        All agents use ``{conv}:{name}`` format via SessionIdStrategy.
        Peers have their own context manager set directly on their pipeline;
        context_manager_factory is cleared during peer initialization.
        """
        if self.context_manager is None:
            raise RuntimeError("Context manager is not initialized")
        return self.context_manager

    # --- Cleanup ---

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
            ctx = MemoryContext(
                session_id=session_id,
                user_id="default",
                agent_id=sub_name,
                agent_role=MemoryAgentRole.SUBAGENT,
            )
            await memory_system.clear(ctx)
            logger.info("Cleaned up subagent memory for session: %s", session_id)
        except Exception:
            logger.exception("Failed to clean up subagent memory for session: %s", session_id)

    # --- Peer Agent Initialization ---

    async def _initialize_peer_agents(self) -> None:
        if self.agent_pool is None:
            raise RuntimeError("AgentPool is not initialized")
        if self.broker is None:
            raise RuntimeError("Broker is not initialized")
        if self.subagent_manager is None:
            raise RuntimeError("SubagentManager is not initialized")

        multi_agent_config = self.config.get("multi_agent", {})
        peers_config = multi_agent_config.get("peers", [])
        parent_name = multi_agent_config.get("parent_agent_name", "main")

        if not peers_config:
            print("[INFO] No peer agents configured")
            return

        if self.mode != "pool":
            print(
                f"   [WARN] {len(peers_config)} peer agents configured but mode='{self.mode}'. "
                "Peer agents require pool mode."
            )
            return

        print(f"\n[INIT] Initializing {len(peers_config)} peer agents...")

        for peer_config in peers_config:
            peer_name = peer_config.get("name", "unnamed")
            print(f"[INIT] Initializing peer agent: {peer_name}")

            # 1. Build descriptor + tool_manager + skill_manager
            descriptor, tool_manager, skill_manager = await self._build_peer_descriptor(peer_config)

            # 2. Framework-level validation
            from framework.multi_agent.peer_validator import PeerAgentValidator

            PeerAgentValidator.validate(descriptor, parent_name)

            # 3. Create independent persistent memory
            context_manager = await self._create_peer_memory(peer_name, peer_config)
            print(f"   [OK] MemorySystem initialized (persistent, storage: data/memory/peers/{peer_name})")

            # 4. Register send_message_async (star topology: peer -> main only)
            # Uses framework SendMessageAsyncTool with configurable wakeup_timeout.
            # After send_silent(), if the message is not consumed within the timeout,
            # a broker wakeup signal is automatically sent to trigger inbox consumption.
            peer_address = AgentAddress(name=peer_name)
            async_send_timeout = peer_config.get("async_send_timeout", 5.0)
            peer_strategy = DefaultSessionIdStrategy(main_agent_name=parent_name)
            async_send_tool = SendMessageAsyncTool(
                broker=self.broker,
                self_address=peer_address,
                allowed_targets=[parent_name],
                agent_bus=self.agent_bus,
                registry=self.agent_pool,
                wakeup_timeout=async_send_timeout,
                session_strategy=peer_strategy,
            )
            tool_manager.register(async_send_tool)
            print(f"   [OK] send_message_async registered (allowed_targets=[{parent_name}], wakeup_timeout={async_send_timeout}s)")

            # 5. Register subagent tool for this peer (sync only)
            if peer_config.get("subagent", {}).get("enabled", False):
                sub_sync = multi_agent_config.get("subagent_sync", {})
                if sub_sync.get("enabled", True):
                    sub_descriptor, sub_tm, sub_sm = await self._build_subagent_descriptor(sub_sync)
                    spawn_tool = SpawnSubagentTool(
                        manager=self.subagent_manager,
                        default_parent_address=peer_address,
                        descriptor=sub_descriptor,
                        tool_manager=sub_tm,
                        skill_manager=sub_sm,
                        broker=self.broker,
                        agent_bus=self.agent_bus,
                        registry=self.agent_pool,
                    )
                    tool_manager.register(spawn_tool)
                    print(f"   [OK] Peer subagent_sync registered (subagent={sub_descriptor.address.name})")

            # 6. Register as resident agent
            await self.agent_pool.register_resident(
                descriptor,
                context_manager=context_manager,
                tool_manager=tool_manager,
                skill_manager=skill_manager,
                output_adapter=NullOutputAdapter(),
            )

            # 7. Clear context_manager_factory so peer uses its own context manager
            instance = self.agent_pool.get(peer_name)
            if instance and instance.pipeline:
                instance.pipeline.context_manager_factory = None
                print("   [OK] Cleared context_manager_factory, using peer-independent context manager")

            # 8. Inject PeerAutoSendHook as safety net
            if self.agent_bus is not None and instance and instance.pipeline:
                from framework.multi_agent.hooks import PeerAutoSendHook

                instance.pipeline.hooks.append(
                    PeerAutoSendHook(
                        agent_bus=self.agent_bus,
                        self_name=peer_name,
                        parent_name=parent_name,
                    )
                )
                print(f"   [OK] PeerAutoSendHook injected (peer={peer_name} -> main)")

            print(f"[OK] Peer agent '{peer_name}' registered as resident")

        print(f"[OK] {len(peers_config)} peer agents initialized\n")
