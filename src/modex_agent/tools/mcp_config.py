"""MCP server configuration.

MCP is a source of Tool objects, not an agent-level capability.
Declare servers here; the factory connects, converts tools, and
injects them into the tool manager for agent selection in code.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MCPServerEntry(BaseModel):
    """Configuration for a single MCP server connection.

    ``transport`` accepts the legacy alias ``type`` on input (matching the
    Claude ``mcp.json`` convention ``{"type": "sse", ...}``), and serializes
    as ``transport`` to match the runtime ``MCPClientManager`` API.

    Transport spellings accepted by the load path (``streamable_http``,
    ``streamable-http``, ``streamablehttp``, ``http``, ``local``) are
    normalized to the canonical Literal form here so the model is the single
    authority on transport vocabulary — reusing ``_TRANSPORT_ALIASES`` from
    the MCP client would be ideal, but a string-level normalizer keeps this
    config module free of a runtime-client import.

    ``command`` accepts both ``str`` and ``list[str]``.  When a list is
    given, the first element becomes the command and the rest are merged
    with ``args``.

    ``environment`` is accepted as an alias for ``env`` on input.
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", populate_by_name=True
    )

    transport: Literal["stdio", "sse", "streamableHttp"] | None = Field(default=None, alias="type")
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
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
        if low == "local":
            return "stdio"
        return v

    @model_validator(mode="before")
    @classmethod
    def _normalize_input(cls, values: dict) -> dict:
        if not isinstance(values, dict):
            return values

        # ``environment`` alias → ``env``
        if "environment" in values and "env" not in values:
            values["env"] = values.pop("environment")

        # ``command`` as list → split into command + args
        cmd = values.get("command")
        if isinstance(cmd, list):
            if not cmd:
                values["command"] = ""
            else:
                values["command"] = cmd[0]
                extra_args = cmd[1:]
                if extra_args:
                    # Prepend to existing args
                    existing_args = values.get("args", [])
                    if isinstance(existing_args, list):
                        values["args"] = extra_args + existing_args
                    else:
                        values["args"] = extra_args

        return values


class MCPConfig(BaseModel):
    """MCP configuration. None = no MCP servers connected."""

    enabled: bool = True
    config_dir: str = "mcp"
    servers: dict[str, MCPServerEntry] = Field(default_factory=dict)
