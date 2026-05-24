"""MCP server configuration.

MCP is a source of Tool objects, not an agent-level capability.
Declare servers here; the factory connects, converts tools, and
injects them into ToolRegistry for agent selection in code.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MCPServerEntry(BaseModel):
    """Configuration for a single MCP server connection.

    `transport` accepts the legacy alias `type` on input (matching the
    Claude `mcp.json` convention `{"type": "sse", ...}`), and serializes
    as `transport` to match the runtime `MCPClientManager` API.
    """

    model_config = ConfigDict(populate_by_name=True)

    transport: Literal["stdio", "sse", "streamableHttp"] | None = Field(
        default=None, alias="type"
    )
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: int = 30

    @field_validator("transport", mode="before")
    @classmethod
    def _normalize_transport(cls, v: str | None) -> str | None:
        if v is None:
            return v
        low = v.lower().replace("-", "_")
        if low in ("streamable_http", "streamablehttp", "http"):
            return "streamableHttp"
        return v


class MCPConfig(BaseModel):
    """MCP configuration. None = no MCP servers connected."""

    enabled: bool = True
    config_file: str = "mcp.json"
    servers: dict[str, MCPServerEntry] = Field(default_factory=dict)
    tool_prefix: str = "mcp"
