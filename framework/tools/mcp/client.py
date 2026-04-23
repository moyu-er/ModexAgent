"""MCP client implementations using official MCP SDK.

Supports stdio, sse, and streamable_http transports.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from enum import StrEnum
from typing import Any, Dict, List, Optional

import httpx
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
}


class BaseMCPClient(ABC):
    """MCP client base class."""

    def __init__(self, name: str):
        self.name = name
        self.session = None
        self._tools: List[Dict[str, Any]] = []
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

    async def list_tools(self) -> List[Dict[str, Any]]:
        """List available tools."""
        if not self.session:
            return []

        try:
            response = await self.session.list_tools()
            tools = []
            for tool in response.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                })
            self._tools = tools
            return tools
        except Exception as e:
            _logger.debug("[MCP:%s] Failed to list tools: %s", self.name, e)
            return []

    async def list_resources(self) -> List[Dict[str, Any]]:
        """List available resources."""
        if not self.session:
            return []

        try:
            response = await self.session.list_resources()
            resources = []
            for resource in response.resources:
                resources.append({
                    "name": resource.name,
                    "description": resource.description,
                    "uri": resource.uri,
                    "mimeType": getattr(resource, "mimeType", None),
                })
            return resources
        except Exception as e:
            _logger.debug("[MCP:%s] Failed to list resources: %s", self.name, e)
            return []

    async def list_prompts(self) -> List[Dict[str, Any]]:
        """List available prompts."""
        if not self.session:
            return []

        try:
            response = await self.session.list_prompts()
            prompts = []
            for prompt in response.prompts:
                prompts.append({
                    "name": prompt.name,
                    "description": prompt.description,
                    "arguments": [
                        {
                            "name": arg.name,
                            "description": getattr(arg, "description", None),
                            "required": getattr(arg, "required", False),
                        }
                        for arg in (prompt.arguments or [])
                    ],
                })
            return prompts
        except Exception as e:
            _logger.debug("[MCP:%s] Failed to list prompts: %s", self.name, e)
            return []

    async def call_tool(
        self,
        tool_name: str,
        params: Dict[str, Any],
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> Dict[str, Any]:
        """Call an MCP tool."""
        if not self.session:
            return {"success": False, "error": "Not connected"}

        try:
            result = await asyncio.wait_for(
                self.session.call_tool(tool_name, arguments=params),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _logger.debug("[MCP:%s] Tool '%s' timed out after %ds", self.name, tool_name, timeout)
            return {"success": False, "error": "Tool call timed out after %ds" % timeout}
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            _logger.debug("[MCP:%s] Tool '%s' was cancelled", self.name, tool_name)
            return {"success": False, "error": "Tool call was cancelled"}
        except McpError as exc:
            _logger.error(
                "[MCP:%s] Tool '%s' MCP error: code=%s message=%s",
                self.name, tool_name, exc.error.code, exc.error.message,
            )
            return {"success": False, "error": "MCP error [%s]: %s" % (exc.error.code, exc.error.message)}
        except Exception as e:
            _logger.debug("[MCP:%s] Tool '%s' failed: %s: %s", self.name, tool_name, type(e).__name__, e)
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

    async def read_resource(
        self,
        uri: str,
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> Dict[str, Any]:
        """Read an MCP resource."""
        if not self.session:
            return {"success": False, "error": "Not connected"}

        try:
            result = await asyncio.wait_for(
                self.session.read_resource(uri),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _logger.debug("[MCP:%s] Resource '%s' timed out after %ds", self.name, uri, timeout)
            return {"success": False, "error": "Resource read timed out after %ds" % timeout}
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            _logger.debug("[MCP:%s] Resource '%s' was cancelled", self.name, uri)
            return {"success": False, "error": "Resource read was cancelled"}
        except McpError as exc:
            _logger.error(
                "[MCP:%s] Resource '%s' MCP error: code=%s message=%s",
                self.name, uri, exc.error.code, exc.error.message,
            )
            return {"success": False, "error": "MCP error [%s]: %s" % (exc.error.code, exc.error.message)}
        except Exception as e:
            _logger.debug("[MCP:%s] Resource '%s' failed: %s: %s", self.name, uri, type(e).__name__, e)
            return {"success": False, "error": str(e)}

        parts = []
        for block in result.contents:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "blob"):
                parts.append("[Binary resource: %d bytes]" % len(block.blob))
            else:
                parts.append(str(block))

        return {
            "success": True,
            "result": "\n".join(parts) or "(no output)",
        }

    async def get_prompt(
        self,
        prompt_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> Dict[str, Any]:
        """Get an MCP prompt."""
        if not self.session:
            return {"success": False, "error": "Not connected"}

        try:
            result = await asyncio.wait_for(
                self.session.get_prompt(prompt_name, arguments=arguments),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            _logger.debug("[MCP:%s] Prompt '%s' timed out after %ds", self.name, prompt_name, timeout)
            return {"success": False, "error": "Prompt call timed out after %ds" % timeout}
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling() > 0:
                raise
            _logger.debug("[MCP:%s] Prompt '%s' was cancelled", self.name, prompt_name)
            return {"success": False, "error": "Prompt call was cancelled"}
        except McpError as exc:
            _logger.error(
                "[MCP:%s] Prompt '%s' MCP error: code=%s message=%s",
                self.name, prompt_name, exc.error.code, exc.error.message,
            )
            return {"success": False, "error": "MCP error [%s]: %s" % (exc.error.code, exc.error.message)}
        except Exception as e:
            _logger.debug("[MCP:%s] Prompt '%s' failed: %s: %s", self.name, prompt_name, type(e).__name__, e)
            return {"success": False, "error": str(e)}

        parts = []
        for message in result.messages:
            content = message.content
            if hasattr(content, "text"):
                parts.append(content.text)
            elif isinstance(content, list):
                for block in content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))
            else:
                parts.append(str(content))

        return {
            "success": True,
            "result": "\n".join(parts) or "(no output)",
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
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
    ):
        super().__init__(name)
        self.command = command
        self.args = args or []
        self.env = env or {}

    async def initialize(self) -> bool:
        """Initialize subprocess connection."""
        try:
            from mcp import ClientSession
            from mcp.client.stdio import stdio_client
            from mcp import StdioServerParameters

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
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(name)
        self.url = url
        self.headers = headers or {}

    async def initialize(self) -> bool:
        """Initialize SSE connection."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client

            def httpx_client_factory(
                headers: Optional[Dict[str, str]] = None,
                timeout: Optional[httpx.Timeout] = None,
                auth: Optional[httpx.Auth] = None,
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
        headers: Optional[Dict[str, str]] = None,
    ):
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
            self.session = await self._exit_stack.enter_async_context(
                ClientSession(read, write)
            )

            await self.session.initialize()
            self._initialized = True

            _logger.debug("[MCP:%s] Connected via Streamable HTTP to %s", self.name, self.url)
            return True

        except Exception as e:
            _logger.debug("[MCP:%s] Failed to initialize HTTP connection: %s", self.name, e)
            await self.close()
            return False
