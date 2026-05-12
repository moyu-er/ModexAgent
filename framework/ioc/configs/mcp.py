"""MCP server configuration.

MCP is a source of Tool objects, not an agent-level capability.
Declare servers here; the factory connects, converts tools, and
injects them into ToolRegistry for agent selection in code.
"""

from typing import Literal

from pydantic import BaseModel, Field


class MCPServerEntry(BaseModel):
    """Configuration for a single MCP server connection."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: int = 30


class MCPConfig(BaseModel):
    """MCP configuration. None = no MCP servers connected."""

    servers: dict[str, MCPServerEntry] = Field(default_factory=dict)
    tool_prefix: str = "mcp"
