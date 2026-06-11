"""AppFactory — creates a complete app from AppConfig.

This is the single entry point that replaces BotService.initialize().
"""

from __future__ import annotations

import logging
from pathlib import Path

from framework.core.provider import LLMProvider
from framework.core.tool_manager import InMemoryToolManager, Tool
from framework.ioc.configs.app import AppConfig
from framework.ioc.factories.agent import create_agent
from framework.ioc.factories.llm import create_llm_provider
from framework.ioc.factories.memory import DefaultMemorySystem, create_memory
from framework.ioc.factories.tools import connect_mcp, create_tool_manager, register_mcp_tools
from framework.tools.mcp_adapter import MCPToolAdapter

logger = logging.getLogger(__name__)


class App:
    """A fully assembled application ready to run."""

    def __init__(self) -> None:
        self.agents: dict[str, ReActAgent] = {}
        self.tool_managers: dict[str, InMemoryToolManager] = {}
        self.memory_system: DefaultMemorySystem | None = None
        self.config: AppConfig | None = None
        self._mcp_adapter: MCPToolAdapter | None = None

    async def start(self) -> None:
        """Start the application."""
        logger.info("App started with %d agents", len(self.agents))

    async def stop(self) -> None:
        """Stop the application, disconnecting MCP servers."""
        if self._mcp_adapter is not None:
            try:
                await self._mcp_adapter.mcp_manager.disconnect_all()
            except Exception:
                logger.warning("MCP disconnect error", exc_info=True)


async def create_app(
    cfg: AppConfig,
    *,
    project_dir: str | Path = ".",
    agent_tools: dict[str, list[Tool]] | None = None,
) -> App:
    """Create a fully assembled App from AppConfig.

    This function replaces the 400+ line BotService.initialize().

    Args:
        cfg: The full application configuration.
        project_dir: Project root directory for resolving relative paths.
        agent_tools: Optional dict of agent_name → list of Tool objects
            provided by business-layer code. MCP tools are added
            automatically.

    Returns:
        A fully assembled App ready to start.
    """
    app = App()
    app.config = cfg

    # 1. Master LLM provider (shared default)
    master_llm: LLMProvider = create_llm_provider(cfg.llm, cfg.safety)

    # 2. MCP tools (if configured)
    app._mcp_adapter = await connect_mcp(cfg.mcp)

    # 3. Memory system (if configured)
    if cfg.memory is not None:
        memory_dir = Path(project_dir) / cfg.paths.memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)
        app.memory_system = create_memory(cfg.memory, master_llm, memory_dir)

    # 4. Create agents
    agent_tools = agent_tools or {}
    for agent_cfg in cfg.agents:
        agent = create_agent(
            agent_cfg,
            default_llm_provider=master_llm,
            default_safety=cfg.safety,
        )

        # Resolve tools: business-layer tools + MCP tools
        tools: list[Tool] = list(agent_tools.get(agent_cfg.name, []))
        tm = create_tool_manager(tools)

        # Register MCP tools into agent's tool manager
        if app._mcp_adapter is not None:
            _ = await register_mcp_tools(app._mcp_adapter, tm)

        app.agents[agent_cfg.name] = agent
        app.tool_managers[agent_cfg.name] = tm

    return app


# Import at end for type annotation
from framework.agents.react import ReActAgent  # noqa: E402
