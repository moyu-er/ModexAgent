"""MCP tool adapter.

Bridges MCP capabilities to the framework's Tool interface.
Re-exports from framework.tools.mcp for backward compatibility.
"""

import logging

from framework.tools.mcp import (
    MCPClientManager,
    MCPPromptTool,
    MCPResourceTool,
)
from framework.tools.mcp import (
    MCPTool as _MCPTool,
)
from framework.tools.mcp.client import _DEFAULT_TOOL_TIMEOUT

from .registry import ToolRegistry

logger = logging.getLogger(__name__)


MCPTool = _MCPTool


class MCPToolAdapter:
    """MCP tool adapter.

    Converts MCP server capabilities into framework Tool objects
    and registers them to ToolRegistry.

    Supports:
    - Config-driven MCP server connections
    - Tool filtering (whitelist/blacklist)
    - Tool name prefixing (to avoid conflicts)
    - Auto-reconnect and error handling

    Example:
        >>> from framework.tools.mcp import MCPClientManager
        >>> manager = MCPClientManager(config=servers_config)
        >>> await manager.initialize()
        >>>
        >>> adapter = MCPToolAdapter(mcp_manager=manager)
        >>> await adapter.register_tools(registry)
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager | None = None,
        default_prefix: bool = True,
        tool_timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ):
        """Initialize MCP adapter.

        Args:
            mcp_manager: MCP client manager instance
            default_prefix: whether to prefix tool names with server name (default True)
            tool_timeout: default timeout for tool calls in seconds
        """
        if mcp_manager is None:
            raise ValueError("mcp_manager is required")
        self.mcp_manager = mcp_manager
        self.default_prefix = default_prefix
        self.tool_timeout = tool_timeout

    async def register_tools(
        self,
        registry: ToolRegistry,
        server_filter: list[str] | None = None,
        tool_filter: dict[str, list[str]] | None = None,
        prefix: bool | None = None,
    ) -> list[str]:
        """Register MCP tools to ToolRegistry.

        Args:
            registry: tool registry
            server_filter: server whitelist, None means all servers
            tool_filter: tool whitelist, format {server_name: [tool_names]}
            prefix: whether to add server prefix, None uses default_prefix

        Returns:
            list of registered tool names
        """
        use_prefix = prefix if prefix is not None else self.default_prefix
        registered: list[str] = []

        for server_name in self.mcp_manager.connected_servers:
            if server_filter and server_name not in server_filter:
                continue

            try:
                tools = await self.mcp_manager.list_tools(server_name)
            except Exception as e:
                logger.warning("Failed to list tools from %s: %s", server_name, e)
                continue

            allowed_tools = None
            if tool_filter and server_name in tool_filter:
                allowed_tools = set(tool_filter[server_name])

            for tool_info in tools:
                tool_name = tool_info["name"]

                if allowed_tools and tool_name not in allowed_tools:
                    continue

                parameters = tool_info.get("inputSchema", {"type": "object", "properties": {}})

                tool = MCPTool(
                    server_name=server_name,
                    tool_name=tool_name,
                    description="[%s] %s" % (server_name, tool_info.get("description", "")),
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
                        description=resource.get("description", resource["name"]),
                        mcp_manager=self.mcp_manager,
                        resource_timeout=self.tool_timeout,
                    )
                    try:
                        registry.register(resource_tool)
                        registered.append(resource_tool.name)
                    except ValueError as e:
                        logger.debug("Failed to register resource tool %s: %s", resource_tool.name, e)
            except Exception as e:
                logger.debug("Failed to list resources from %s: %s", server_name, e)

            try:
                prompts = await self.mcp_manager.list_prompts(server_name)
                for prompt in prompts:
                    prompt_tool = MCPPromptTool(
                        server_name=server_name,
                        prompt_name=prompt["name"],
                        description=prompt.get("description", prompt["name"]),
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
        await self.mcp_manager.disconnect_all()


class MCPToolRegistry(ToolRegistry):
    """ToolRegistry with MCP integration.

    Automatically loads MCP servers from config and registers tools.

    Example:
        >>> registry = MCPToolRegistry(mcp_manager=manager)
        >>> await registry.initialize_from_config()
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager | None = None,
        tool_timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ):
        super().__init__()
        self._mcp_adapter: MCPToolAdapter | None = None
        self._mcp_manager = mcp_manager
        self._tool_timeout = tool_timeout

    async def initialize_from_config(
        self,
        server_filter: list[str] | None = None,
        tool_filter: dict[str, list[str]] | None = None,
    ) -> list[str]:
        """Initialize MCP tools from config.

        Args:
            server_filter: server whitelist
            tool_filter: tool whitelist

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
        return await self._mcp_adapter.register_tools(
            self,
            server_filter=server_filter,
            tool_filter=tool_filter,
        )

    async def close(self) -> None:
        """Close MCP connections."""
        if self._mcp_adapter:
            await self._mcp_adapter.close()
