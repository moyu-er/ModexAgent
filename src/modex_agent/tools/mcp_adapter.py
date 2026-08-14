"""MCP tool adapter.

Bridges MCP capabilities to the framework's Tool interface.
Re-exports from framework.tools.mcp for backward compatibility.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.tools.mcp import (
    MCPPromptTool,
    MCPResourceTool,
)
from modex_agent.tools.mcp import (
    MCPTool as _MCPTool,
)
from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import _DEFAULT_TOOL_TIMEOUT

from .registry import ToolRegistry

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import Tool

logger = logging.getLogger(__name__)


MCPTool = _MCPTool


class MCPToolAdapter:
    """MCP tool adapter.

    Converts MCP server capabilities into framework Tool objects
    and registers them to ToolRegistry.

    Supports:
    - Config-driven MCP server connections
    - Tool name prefixing (to avoid conflicts)
    - Auto-reconnect and error handling

    Example:
        >>> from modex_agent.tools.mcp import MCPClientManager
        >>> manager = MCPClientManager(config=servers_config)
        >>> await manager.initialize()
        >>>
        >>> adapter = MCPToolAdapter(mcp_manager=manager)
        >>> await adapter.register_tools(registry)
    """

    def __init__(
        self,
        mcp_manager: McpBackend | None = None,
        default_prefix: bool = True,
        tool_timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        if mcp_manager is None:
            raise ValueError("mcp_manager is required")
        self.mcp_manager: McpBackend = mcp_manager
        self.default_prefix = default_prefix
        self.tool_timeout = tool_timeout

    async def register_tools(
        self,
        registry: ToolRegistry,
        prefix: bool | None = None,
    ) -> list[str]:
        """Register all connected MCP servers' tools to ToolRegistry.

        Args:
            registry: tool registry
            prefix: whether to add server prefix, None uses default_prefix

        Returns:
            list of registered tool names
        """
        use_prefix = prefix if prefix is not None else self.default_prefix
        registered: list[str] = []

        for server_name in self.mcp_manager.connected_servers:
            try:
                tools = await self.mcp_manager.list_tools(server_name)
            except Exception as e:
                logger.warning("Failed to list tools from %s: %s", server_name, e)
                continue

            for tool_info in tools:
                tool_name = tool_info["name"]
                parameters = tool_info.get("inputSchema", {"type": "object", "properties": {}})

                tool = MCPTool(
                    server_name=server_name,
                    tool_name=tool_name,
                    description="[{}] {}".format(server_name, tool_info.get("description", "")),
                    parameters=parameters,
                    mcp_manager=self.mcp_manager,
                    tool_timeout=self.tool_timeout,
                    use_prefix=use_prefix,
                )

                try:
                    registry.register(tool)
                    registered.append(tool.name)
                except ValueError as e:
                    logger.warning("Failed to register %s: %s", tool.name, e)

            try:
                resources = await self.mcp_manager.list_resources(server_name)
                for resource in resources:
                    resource_tool = MCPResourceTool(
                        server_name=server_name,
                        resource_name=resource["name"],
                        uri=resource["uri"],
                        description=str(resource.get("description", resource["name"])),
                        mcp_manager=self.mcp_manager,
                        resource_timeout=self.tool_timeout,
                    )
                    try:
                        registry.register(resource_tool)
                        registered.append(resource_tool.name)
                    except ValueError as e:
                        logger.debug(
                            "Failed to register resource tool %s: %s", resource_tool.name, e
                        )
            except Exception as e:
                logger.debug("Failed to list resources from %s: %s", server_name, e)

            try:
                prompts = await self.mcp_manager.list_prompts(server_name)
                for prompt in prompts:
                    prompt_tool = MCPPromptTool(
                        server_name=server_name,
                        prompt_name=prompt["name"],
                        description=str(prompt.get("description", prompt["name"])),
                        arguments_def=prompt.get("arguments", []),
                        mcp_manager=self.mcp_manager,
                        prompt_timeout=self.tool_timeout,
                    )
                    try:
                        registry.register(prompt_tool)
                        registered.append(prompt_tool.name)
                    except ValueError as e:
                        logger.debug("Failed to register prompt tool %s: %s", prompt_tool.name, e)
            except Exception as e:
                logger.debug("Failed to list prompts from %s: %s", server_name, e)

        return registered

    async def close(self) -> None:
        """Close all MCP connections."""
        await self.mcp_manager.release()


async def acquire_mcp_tools(
    backend: McpBackend,
    *,
    tool_timeout: int = _DEFAULT_TOOL_TIMEOUT,
) -> list[Tool]:
    """Adapt a connected ``McpBackend`` into a flat list of framework ``Tool``s.

    Wraps ``backend`` in an :class:`MCPToolAdapter`, registers every server's
    tools/resources/prompts into a fresh :class:`ToolRegistry`, and returns the
    collected ``Tool`` objects. Shared by the bot main-agent path
    (``bot.service.builders._load_agent_mcp_tools``) and the framework subagent
    path (``tools.mcp_loader.load_per_agent_mcp``) so the
    acquire → adapt → register → collect dance lives in one place.

    The caller owns the ``backend`` lifecycle: a ``SharedMcpBackend`` is merely
    detached on ``release()`` (shared connections outlive it), while a private
    ``MCPClientManager`` is closed by its own ``release()``.
    """
    adapter = MCPToolAdapter(
        mcp_manager=backend, default_prefix=True, tool_timeout=tool_timeout
    )
    registry = ToolRegistry()
    await adapter.register_tools(registry=registry)
    tools: list[Tool] = []
    for name in registry.list_tools():
        tool = registry.get_tool(name)
        if tool is not None:
            tools.append(tool)
    return tools


class MCPToolRegistry(ToolRegistry):
    """ToolRegistry with MCP integration.

    Automatically loads MCP servers from config and registers tools.

    Example:
        >>> registry = MCPToolRegistry(mcp_manager=manager)
        >>> await registry.initialize_from_config()
    """

    def __init__(
        self,
        mcp_manager: McpBackend | None = None,
        tool_timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        super().__init__()
        self._mcp_adapter: MCPToolAdapter | None = None
        self._mcp_manager = mcp_manager
        self._tool_timeout = tool_timeout

    async def initialize_from_config(self) -> list[str]:
        """Initialize MCP tools from config.

        Returns:
            list of registered tool names
        """
        if self._mcp_manager is None:
            logger.warning("MCP manager not provided, skipping MCP initialization")
            return []

        self._mcp_adapter = MCPToolAdapter(
            mcp_manager=self._mcp_manager,
            tool_timeout=self._tool_timeout,
        )
        return await self._mcp_adapter.register_tools(self)

    async def close(self) -> None:
        """Close MCP connections."""
        if self._mcp_adapter:
            await self._mcp_adapter.close()
