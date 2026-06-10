"""Tool-related factories.

MCP servers → Tool registry injection.
This is pure code-side — no YAML involved in tool selection.
"""

from __future__ import annotations

from framework.core.tool_manager import InMemoryToolManager, Tool, ToolManagerConfig
from framework.ioc.configs.mcp import MCPConfig


async def connect_mcp(
    mcp_config: MCPConfig | None,
) -> MCPToolAdapter | None:
    """Connect to MCP servers and return an MCPToolAdapter ready for registration.

    Args:
        mcp_config: MCP configuration. None or empty servers = no connection.

    Returns:
        MCPToolAdapter or None if no MCP servers configured.
    """
    if mcp_config is None or not mcp_config.servers:
        return None

    from framework.tools.mcp import MCPClientManager
    from framework.tools.mcp_adapter import MCPToolAdapter

    servers_dict: dict[str, object] = {
        name: entry.model_dump(exclude_none=True)
        for name, entry in mcp_config.servers.items()
    }

    manager = MCPClientManager(config=servers_dict)
    await manager.initialize()

    return MCPToolAdapter(
        mcp_manager=manager,
        default_prefix=True,
    )


async def register_mcp_tools(
    adapter: MCPToolAdapter | None,
    tool_manager: InMemoryToolManager,
) -> list[str]:
    """Register MCP tools from adapter into a tool manager.

    Args:
        adapter: MCPToolAdapter with connected MCP servers.
        tool_manager: Target InMemoryToolManager.

    Returns:
        List of registered tool names.
    """
    if adapter is None:
        return []

    from framework.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = await adapter.register_tools(registry=registry)
    for name in names:
        tool = registry.get(name)
        if tool is not None:
            tool_manager.register(tool)
    return names


def create_tool_manager(
    tools: list[Tool],
    max_workers: int = 10,
) -> InMemoryToolManager:
    """Create an InMemoryToolManager pre-populated with the given tools.

    Args:
        tools: List of Tool objects from framework, MCP, or business code.
        max_workers: Max concurrent tool executions.

    Returns:
        Configured InMemoryToolManager with all tools registered.
    """
    tm = InMemoryToolManager(
        config=ToolManagerConfig(
            max_workers=max_workers,
            enable_parallel=True,
            parallel_max_workers=5,
        )
    )
    for tool in tools:
        tm.register(tool)
    return tm
