"""MCP client implementations using official MCP SDK.

Supports stdio, sse, and streamable_http transports.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from enum import StrEnum
from typing import Any

import httpx
from mcp import ClientSession
from mcp.shared.exceptions import McpError

_logger = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT = 30

_STDIO_POLLUTION_MARKERS = (
    "parse error",
    "invalid json",
    "unexpected token",
    "jsonrpc",
    "content-length",
)


class TransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamableHttp"


_TRANSPORT_ALIASES = {
    "http": TransportType.STREAMABLE_HTTP,
    "streamable_http": TransportType.STREAMABLE_HTTP,
    "streamablehttp": TransportType.STREAMABLE_HTTP,
    "streamable-http": TransportType.STREAMABLE_HTTP,
}


class BaseMCPClient(ABC):
    """MCP client base class."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []
        self._initialized = False
        self._managed_externally = False
        self._exit_stack = AsyncExitStack()

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize connection."""
        pass

    async def close(self) -> None:
        """Close connection."""
        if self._managed_externally:
            self.session = None
            self._initialized = False
            return
        try:
            await self._exit_stack.aclose()
        except Exception as e:
            _logger.debug("[MCP:%s] Error during close: %s", self.name, e)
        finally:
            self.session = None
            self._tools = []
            self._initialized = False

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available tools."""
        if not self.session:
            return []

        try:
            response = await self.session.list_tools()
            tools = []
            for tool in response.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                    }
                )
            self._tools = tools
            return tools
        except Exception as e:
            _logger.debug("[MCP:%s] Failed to list tools: %s", self.name, e)
            return []

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Call an MCP tool."""
        if not self.session:
            return {"success": False, "error": "Not connected"}

        try:
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments=params),
                timeout=timeout,
            )
        except TimeoutError:
            _logger.debug("[MCP:%s] Tool '%s' timed out after %ds", self.name, tool_name, timeout)
            return {"success": False, "error": f"Tool call timed out after {timeout:d}s"}
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            _logger.debug("[MCP:%s] Tool '%s' was cancelled", self.name, tool_name)
            return {"success": False, "error": "Tool call was cancelled"}
        except McpError as exc:
            _logger.error(
                "[MCP:%s] Tool '%s' MCP error: code=%s message=%s",
                self.name,
                tool_name,
                exc.error.code,
                exc.error.message,
            )
            return {
                "success": False,
                "error": f"MCP error [{exc.error.code}]: {exc.error.message}",
            }
        except Exception as e:
            _logger.debug(
                "[MCP:%s] Tool '%s' failed: %s: %s", self.name, tool_name, type(e).__name__, e
            )
            return {"success": False, "error": str(e)}

        content_parts = []
        for content in result.content:
            if hasattr(content, "text"):
                content_parts.append(content.text)
            else:
                content_parts.append(str(content))

        output = "\n".join(content_parts) or "(no output)"

        if getattr(result, "isError", False):
            return {
                "success": False,
                "error": output,
                "isError": True,
            }

        return {
            "success": True,
            "result": output,
            "isError": False,
        }

    @property
    def is_connected(self) -> bool:
        """Whether connected."""
        return self.session is not None and self._initialized


class StdioMCPClient(BaseMCPClient):
    """MCP client using stdio transport."""

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name)
        self.command = command
        self.args = args or []
        self.env = env or {}

    async def initialize(self) -> bool:
        """Initialize subprocess connection."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            server_params = StdioServerParameters(
                command=self.command,
                args=self.args,
                env=self.env or None,
            )

            stdio_transport = await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(stdio_transport[0], stdio_transport[1])
            )

            await self.session.initialize()
            self._initialized = True

            _logger.debug("[MCP:%s] Connected via stdio", self.name)
            return True

        except Exception as e:
            _logger.debug("[MCP:%s] Failed to initialize: %s", self.name, e)
            await self.close()
            return False


class SSEMCPClient(BaseMCPClient):
    """MCP client using SSE transport."""

    def __init__(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name)
        self.url = url
        self.headers = headers or {}

    async def initialize(self) -> bool:
        """Initialize SSE connection."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            def httpx_client_factory(
                headers: dict[str, str] | None = None,
                timeout: httpx.Timeout | None = None,
                auth: httpx.Auth | None = None,
            ) -> httpx.AsyncClient:
                merged_headers = {
                    "Accept": "application/json, text/event-stream",
                    **(self.headers or {}),
                    **(headers or {}),
                }
                return httpx.AsyncClient(
                    headers=merged_headers or None,
                    follow_redirects=True,
                    timeout=timeout,
                    auth=auth,
                )

            sse_transport = await self._exit_stack.enter_async_context(
                sse_client(self.url, httpx_client_factory=httpx_client_factory)
            )
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(sse_transport[0], sse_transport[1])
            )

            await self.session.initialize()
            self._initialized = True

            _logger.debug("[MCP:%s] Connected via SSE to %s", self.name, self.url)
            return True

        except Exception as e:
            _logger.debug("[MCP:%s] Failed to initialize SSE connection: %s", self.name, e)
            await self.close()
            return False


class StreamableHttpMCPClient(BaseMCPClient):
    """MCP client using Streamable HTTP transport."""

    def __init__(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(name)
        self.url = url
        self.headers = headers or {}

    async def initialize(self) -> bool:
        """Initialize Streamable HTTP connection."""
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client

            http_client = await self._exit_stack.enter_async_context(
                httpx.AsyncClient(
                    headers=self.headers or None,
                    follow_redirects=True,
                    timeout=None,
                )
            )
            read, write, _ = await self._exit_stack.enter_async_context(
                streamable_http_client(self.url, http_client=http_client)
            )
            self.session = await self._exit_stack.enter_async_context(ClientSession(read, write))

            await self.session.initialize()
            self._initialized = True

            _logger.debug("[MCP:%s] Connected via Streamable HTTP to %s", self.name, self.url)
            return True

        except Exception as e:
            _logger.debug("[MCP:%s] Failed to initialize HTTP connection: %s", self.name, e)
            await self.close()
            return False
