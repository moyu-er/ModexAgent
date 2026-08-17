"""MCP backend ABC.

Defines the surface that consumers (``MCPToolAdapter``, ``MCPTool``) depend on,
independent of how connections are owned. ``MCPClientManager`` (owning backend)
implements it; a future shared-connection facade will implement it too — two
backends justify the seam (architecture rule 6).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from modex_agent.tools.mcp.client import _DEFAULT_TOOL_TIMEOUT, BaseMCPClient


class McpBackend(ABC):
    """Abstract MCP backend surface.

    Subclasses implement connection ownership (abstract members); the query /
    invocation members below are pure delegation and are inherited unchanged
    by every backend.
    """

    # -- connection ownership: differs per implementation ---------------------

    @property
    @abstractmethod
    def connected_servers(self) -> list[str]:
        """Names of currently connected servers."""
        raise NotImplementedError

    @abstractmethod
    def _client_for(self, name: str) -> BaseMCPClient | None:
        """Return the client for ``name``, or ``None`` if not connected."""
        raise NotImplementedError

    @abstractmethod
    async def release(self) -> None:
        """Release all backend resources (teardown contract)."""
        raise NotImplementedError

    # -- query / invocation: identical delegation for every backend -----------

    async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
        """List tools on the specified server."""
        client = self._client_for(server_name)
        if not client:
            return []
        return await client.list_tools()

    async def execute_tool(
        self,
        server_name: str,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Execute a tool on the specified server."""
        client = self._client_for(server_name)
        if not client:
            return {"success": False, "error": f"MCP server not connected: {server_name}"}
        return await client.call_tool(tool_name, params, timeout=timeout)
