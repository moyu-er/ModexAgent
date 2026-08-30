"""Tool-related factories.

MCP servers → Tool registry injection.
This is pure code-side — no YAML involved in tool selection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.core.tool_manager import InMemoryToolManager, Tool, ToolManagerConfig
from modex_agent.ioc.configs.mcp import MCPConfig
from modex_agent.tools.mcp_adapter import MCPToolAdapter

if TYPE_CHECKING:
    from modex_agent.tools.mcp.registry import McpConnectionRegistry


async def connect_mcp(
    mcp_config: MCPConfig | None,
    *,
    registry: McpConnectionRegistry | None = None,
) -> MCPToolAdapter | None:
    """Connect to MCP servers and return an MCPToolAdapter ready for registration.

    Args:
        mcp_config: MCP configuration. None or empty servers = no connection.
        registry: Optional shared-connection registry (ADR-0017). When provided,
            the adapter wraps a :class:`SharedMcpBackend` obtained via
            ``registry.acquire`` instead of building a private ``MCPClientManager``;
            the registry owns connection lifecycle. When ``None`` (default),
            today's private-manager path runs byte-for-byte unchanged.

            Assembly-chain contract (ticket 04): callers holding the context
            chain reach this handle at the WORKSPACE layer
            (``WorkspaceContext.mcp_registry``) and pass it here — this
            factory stays decoupled from the chain types; the chain makes
            the handle reachable, it does not change how connections open.

    Returns:
        MCPToolAdapter or None if no MCP servers configured.
    """
    if mcp_config is None or not mcp_config.servers:
        return None

    servers_dict: dict[str, object] = {
        name: entry.model_dump(exclude_none=True) for name, entry in mcp_config.servers.items()
    }

    if registry is not None:
        backend = await registry.acquire(list(servers_dict.keys()))
        return MCPToolAdapter(mcp_manager=backend)

    from modex_agent.tools.mcp import MCPClientManager
    from modex_agent.tools.mcp.injector import JsonFileMCPTransportInjector

    manager = MCPClientManager(
        config=servers_dict,
        injector=JsonFileMCPTransportInjector(),
    )
    await manager.initialize()

    return MCPToolAdapter(
        mcp_manager=manager,
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

    from modex_agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = await adapter.register_tools(registry=registry)
    for name in names:
        tool = registry.tools.get(name)
        if tool is not None:
            tool_manager.register(tool)
    return names


def create_tool_manager(
    tools: list[Tool],
) -> InMemoryToolManager:
    """Create an InMemoryToolManager pre-populated with the given tools.

    Args:
        tools: List of Tool objects from framework, MCP, or business code.

    Returns:
        Configured InMemoryToolManager with all tools registered.
    """
    tm = InMemoryToolManager(config=ToolManagerConfig())
    for tool in tools:
        tm.register(tool)
    return tm
