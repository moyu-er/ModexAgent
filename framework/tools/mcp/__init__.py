"""MCP (Model Context Protocol) integration for agent framework.

Provides:
- MCPClientManager: unified management of MCP server connections
- MCPTool/MCPResourceTool/MCPPromptTool: wraps MCP capabilities as framework Tools
- Support for stdio, sse, and streamable_http transports
"""

from framework.tools.mcp.client import (
    BaseMCPClient,
    SSEMCPClient,
    StdioMCPClient,
    StreamableHttpMCPClient,
)
from framework.tools.mcp.manager import MCPClientManager, MCPConnectionError
from framework.tools.mcp.tool import MCPTool, MCPResourceTool, MCPPromptTool

__all__ = [
    "MCPClientManager",
    "MCPConnectionError",
    "MCPTool",
    "MCPResourceTool",
    "MCPPromptTool",
    "BaseMCPClient",
    "StdioMCPClient",
    "SSEMCPClient",
    "StreamableHttpMCPClient",
]
