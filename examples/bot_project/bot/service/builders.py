"""Agent builder mixin for BotService — tool registration, memory, descriptors.

All tool registration methods use Tool objects directly (code-passed).
No tool configuration is read from YAML/config dicts.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bot.plugins.integration import PluginIntegration

if TYPE_CHECKING:
    from framework.ioc.configs.agent import AgentConfig as IOCAgentConfig
from framework.core.skills import SkillManager
from framework.core.tool_manager import Tool
from framework.ioc.configs.app import AppConfig
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import AgentMessageBus, CommunicationTracker
from framework.pipeline.adapters import OutputAdapter

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
    timeout: int = 300,
) -> Tool:
    from framework.tools.terminal import SubprocessExecutor, SubprocessTool

    return SubprocessTool(executor=SubprocessExecutor(), timeout=timeout)


def _make_search_tools() -> list[Tool]:
    from framework.tools.standard import FindFilesTool, SearchFilesTool

    return [SearchFilesTool(), FindFilesTool()]


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
    """Mixin providing shared fields used by pool-mode BotService.

    All fields below are provided by the host ``BotService`` class.
    They are declared here so the mixin's contract is visible to type checkers and IDEs.
    """

    # ── Fields provided by the host BotService class ──

    # Configuration
    _app_config: AppConfig | None

    # Core components
    output_adapter: OutputAdapter
    broker: InMemoryMessageBroker | None
    agent_bus: AgentMessageBus | None

    communication_tracker: CommunicationTracker | None
    plugin_integration: PluginIntegration | None

    # Subagent caches
    _subagent_skill_managers: dict[str, SkillManager]
    _subagent_memory_systems: dict[str, Any]
    _additional_subagent_memory_systems: dict[str, Any]

    # ── Properties provided by BotService ──

    @property
    def _project_dir(self) -> Path:
        """Project root directory. Implemented by BotService."""
        raise NotImplementedError

    @property
    def _main_agent_cfg(self) -> IOCAgentConfig | None:
        """Main agent config by role. Implemented by BotService."""
        raise NotImplementedError

    # ── Method stubs — implemented by BotService ──

    def _find_subagent_cfg(self) -> IOCAgentConfig | None:
        """Find the first subagent config. Implemented by BotService."""
        raise NotImplementedError
