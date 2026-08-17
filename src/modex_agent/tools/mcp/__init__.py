"""MCP (Model Context Protocol) integration for agent framework.

Provides:
- MCPClientManager: unified management of MCP server connections
- connect_single_server: reusable per-server connect primitive
- MCPTool: wraps MCP capabilities as framework Tools
- Support for stdio, sse, and streamable_http transports
"""

from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import (
    BaseMCPClient,
    SSEMCPClient,
    StdioMCPClient,
    StreamableHttpMCPClient,
)
from modex_agent.tools.mcp.connection import (
    MCPConnectionError,
    connect_single_server,
)
from modex_agent.tools.mcp.manager import MCPClientManager
from modex_agent.tools.mcp.registry import (
    McpConnectionRegistry,
    McpConnectionState,
    SharedMcpBackend,
)
from modex_agent.tools.mcp.tool import MCPTool

__all__ = [
    "MCPClientManager",
    "MCPConnectionError",
    "connect_single_server",
    "McpBackend",
    "MCPTool",
    "BaseMCPClient",
    "StdioMCPClient",
    "SSEMCPClient",
    "StreamableHttpMCPClient",
    "McpConnectionRegistry",
    "SharedMcpBackend",
    "McpConnectionState",
]
