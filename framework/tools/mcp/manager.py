"""MCP Client Manager

Unified management of MCP connections, supporting stdio, sse, and streamable_http transports.
"""

import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from typing import Any

from mcp import ClientSession

from framework.tools.mcp.client import (
    _DEFAULT_TOOL_TIMEOUT,
    _STDIO_POLLUTION_MARKERS,
    _TRANSPORT_ALIASES,
    BaseMCPClient,
    SSEMCPClient,
    StdioMCPClient,
    StreamableHttpMCPClient,
    TransportType,
)

_logger = logging.getLogger(__name__)


class MCPConnectionError(Exception):
    """MCP connection error."""
    pass


class MCPClientManager:
    """MCP client manager.

    Unified management of MCP connections, supporting:
    1. stdio transport (local subprocess)
    2. sse transport (Server-Sent Events)
    3. streamable_http transport (Streamable HTTP)
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize MCP client manager.

        Args:
            config: Custom config dict, format:
                {server_name: {transport, command, args, url, headers, env, enabled, tool_timeout, enabled_tools, ...}}
                If None, uses empty config (servers must be provided explicitly).
        """
        self.clients: dict[str, BaseMCPClient] = {}
        self._server_stacks: dict[str, AsyncExitStack] = {}
        self._initialized = False
        self._custom_config = config or {}
        self._reconnect_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize all configured MCP servers."""
        if self._initialized:
            return

        _logger.info("[MCP Manager] Initializing MCP servers...")

        servers = self._custom_config.items()

        for name, server_config in servers:
            enabled = server_config.get("enabled", True) if isinstance(server_config, dict) else True

            if not enabled:
                _logger.info("[MCP Manager] Skipping disabled server: %s", name)
                continue

            try:
                result = await self._connect_single(name, server_config)
            except Exception as e:
                _logger.error("[MCP Manager] Failed to connect to %s: %s", name, e)
                continue

            if result is not None and result[1] is not None:
                self.clients[result[0]] = result[1]

        self._initialized = True
        _logger.info("[MCP Manager] Initialized %d MCP servers", len(self.clients))

    async def _connect_single(
        self, name: str, server_config: dict[str, Any]
    ) -> tuple[str, BaseMCPClient] | None:
        """Connect to a single MCP server."""
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            transport = server_config.get("transport", "").lower()

            if not transport:
                if server_config.get("command"):
                    transport = TransportType.STDIO
                elif server_config.get("url"):
                    url = server_config["url"]
                    transport = TransportType.SSE if url.rstrip("/").endswith("/sse") else TransportType.STREAMABLE_HTTP
                else:
                    _logger.warning("[MCP:%s] No command or url configured, skipping", name)
                    await server_stack.aclose()
                    return name, None

            transport = _TRANSPORT_ALIASES.get(transport, transport)
            client = await self._create_client(name, transport, server_config, server_stack)

            if client and client.session is not None:
                self._server_stacks[name] = server_stack

                try:
                    tools = await client.list_tools()
                    _logger.info("[MCP:%s] Available tools: %s", name, [t["name"] for t in tools])
                except Exception as e:
                    _logger.warning("[MCP:%s] Failed to list tools: %s", name, e)

                return name, client
            else:
                await server_stack.aclose()
                return name, None

        except Exception as e:
            hint = ""
            text = str(e).lower()
            if any(marker in text for marker in _STDIO_POLLUTION_MARKERS):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )

            # Extract root cause from ExceptionGroup (Python 3.11+)
            root_cause = e
            if hasattr(e, "exceptions") and callable(e.exceptions):
                sub_exceptions = e.exceptions()
                if sub_exceptions:
                    root_cause = sub_exceptions[0]

            _logger.error(
                "[MCP:%s] Failed to connect: %s%s",
                name,
                root_cause,
                hint,
            )
            _logger.debug(
                "[MCP:%s] Full exception traceback:",
                name,
                exc_info=True,
            )
            try:
                await server_stack.aclose()
            except Exception:
                pass
            return name, None

    async def _create_client(
        self,
        name: str,
        transport: str,
        server_config: dict[str, Any],
        server_stack: AsyncExitStack,
    ) -> BaseMCPClient | None:
        """Create MCP client based on transport type."""

        if transport == TransportType.STDIO:
            return await self._create_stdio_client(name, server_config, server_stack)
        elif transport == TransportType.SSE:
            return await self._create_sse_client(name, server_config, server_stack)
        elif transport == TransportType.STREAMABLE_HTTP:
            return await self._create_streamable_http_client(name, server_config, server_stack)
        else:
            raise MCPConnectionError(f"Unknown transport: {transport}")

    async def _create_stdio_client(
        self,
        name: str,
        server_config: dict[str, Any],
        server_stack: AsyncExitStack,
    ) -> BaseMCPClient:
        """Create stdio MCP client."""
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = server_config.get("command")
        if not command:
            raise MCPConnectionError("Command required for stdio transport")

        env = server_config.get("env", {})
        args = server_config.get("args", [])

        params = StdioServerParameters(
            command=command,
            args=args or [],
            env=env or None,
        )

        read, write = await server_stack.enter_async_context(stdio_client(params))
        session = await server_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        client = StdioMCPClient(name=name, command=command, args=args, env=env)
        client.session = session
        client._initialized = True
        client._managed_externally = True
        return client

    async def _create_sse_client(
        self,
        name: str,
        server_config: dict[str, Any],
        server_stack: AsyncExitStack,
    ) -> BaseMCPClient:
        """Create SSE MCP client."""
        import httpx
        from mcp.client.sse import sse_client

        url = server_config.get("url")
        if not url:
            raise MCPConnectionError("URL required for sse transport")

        config_headers = server_config.get("headers", {})

        def httpx_client_factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
        ) -> httpx.AsyncClient:
            merged_headers = {
                "Accept": "application/json, text/event-stream",
                **(config_headers or {}),
                **(headers or {}),
            }
            return httpx.AsyncClient(
                headers=merged_headers or None,
                follow_redirects=True,
                timeout=timeout,
                auth=auth,
            )

        sse_transport = await server_stack.enter_async_context(
            sse_client(url, httpx_client_factory=httpx_client_factory)
        )
        session = await server_stack.enter_async_context(
            ClientSession(sse_transport[0], sse_transport[1])
        )
        await session.initialize()

        client = SSEMCPClient(name=name, url=url, headers=config_headers)
        client.session = session
        client._initialized = True
        client._managed_externally = True
        return client

    async def _create_streamable_http_client(
        self,
        name: str,
        server_config: dict[str, Any],
        server_stack: AsyncExitStack,
    ) -> BaseMCPClient:
        """Create Streamable HTTP MCP client."""
        import httpx
        from mcp.client.streamable_http import streamable_http_client

        url = server_config.get("url")
        if not url:
            raise MCPConnectionError("URL required for http transport")

        headers = server_config.get("headers", {})

        http_client = await server_stack.enter_async_context(
            httpx.AsyncClient(
                headers=headers or None,
                follow_redirects=True,
                timeout=None,
            )
        )
        read, write, _ = await server_stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )
        session = await server_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        client = StreamableHttpMCPClient(name=name, url=url, headers=headers)
        client.session = session
        client._initialized = True
        client._managed_externally = True
        return client

    async def disconnect(self, name: str) -> bool:
        """Disconnect from an MCP server."""
        if name in self.clients:
            client = self.clients[name]
            try:
                await client.close()
            except BaseException:
                _logger.debug("MCP client '%s' close error (expected during shutdown)", name)
            finally:
                del self.clients[name]

            if name in self._server_stacks:
                try:
                    await self._server_stacks[name].aclose()
                except BaseException:
                    _logger.debug("MCP stack '%s' close error (expected during shutdown)", name)
                finally:
                    del self._server_stacks[name]

            _logger.info("[MCP Manager] Disconnected from %s", name)
            return True
        return False

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers sequentially.

        Each AsyncExitStack (which wraps stdio_client / streamable_http_client)
        must be closed in the same asyncio Task that created it — anyio's cancel
        scope tracking enforces this.  Using ``asyncio.gather`` would spawn each
        disconnect in a separate Task, triggering
        ``RuntimeError: Attempted to exit cancel scope in a different task``.
        """
        for name in list(self.clients.keys()):
            with suppress(Exception):
                await self.disconnect(name)
        self._initialized = False

    async def reconnect_with_retry(
        self,
        server_name: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ) -> bool:
        """Reconnect with exponential backoff."""
        for attempt in range(max_retries):
            success = await self.reconnect(server_name)
            if success:
                return True
            if attempt < max_retries - 1:
                delay = min(base_delay * (2 ** attempt), max_delay)
                _logger.info(
                    "[MCP Manager] Retrying %s in %.1fs (attempt %d/%d)",
                    server_name, delay, attempt + 1, max_retries,
                )
                await asyncio.sleep(delay)
        return False

    async def reconnect(self, server_name: str | None = None) -> bool:
        """Reconnect to one or all MCP servers.

        Args:
            server_name: specific server to reconnect, or None for all

        Returns:
            True if at least one reconnection succeeded
        """
        async with self._reconnect_lock:
            if server_name is not None:
                config = self._custom_config.get(server_name)
                if not config:
                    _logger.warning("[MCP Manager] No config for server '%s', cannot reconnect", server_name)
                    return False
                await self.disconnect(server_name)
                result = await self._connect_single(server_name, config)
                if result[1] is not None:
                    self.clients[result[0]] = result[1]
                    _logger.info("[MCP Manager] Reconnected to %s", server_name)
                    return True
                return False

            success = False
            for name, server_config in list(self._custom_config.items()):
                await self.disconnect(name)
                result = await self._connect_single(name, server_config)
                if result[1] is not None:
                    self.clients[result[0]] = result[1]
                    success = True
            return success

    async def execute_tool(
        self,
        server_name: str,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Execute a tool on the specified server."""
        client = self.clients.get(server_name)
        if not client:
            return {"success": False, "error": "MCP server not connected: %s" % server_name}

        return await client.call_tool(tool_name, params, timeout=timeout)

    async def read_resource(
        self,
        server_name: str,
        uri: str,
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Read a resource from the specified server."""
        client = self.clients.get(server_name)
        if not client:
            return {"success": False, "error": "MCP server not connected: %s" % server_name}

        return await client.read_resource(uri, timeout=timeout)

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Get a prompt from the specified server."""
        client = self.clients.get(server_name)
        if not client:
            return {"success": False, "error": "MCP server not connected: %s" % server_name}

        return await client.get_prompt(prompt_name, arguments=arguments, timeout=timeout)

    async def list_tools(self, server_name: str) -> list[dict[str, Any]]:
        """List tools on the specified server."""
        client = self.clients.get(server_name)
        if not client:
            return []

        return await client.list_tools()

    async def list_resources(self, server_name: str) -> list[dict[str, Any]]:
        """List resources on the specified server."""
        client = self.clients.get(server_name)
        if not client:
            return []

        return await client.list_resources()

    async def list_prompts(self, server_name: str) -> list[dict[str, Any]]:
        """List prompts on the specified server."""
        client = self.clients.get(server_name)
        if not client:
            return []

        return await client.list_prompts()

    async def list_all_tools(self) -> dict[str, list[dict[str, Any]]]:
        """List tools on all servers."""
        result = {}
        for name, client in self.clients.items():
            try:
                tools = await client.list_tools()
                result[name] = tools
            except Exception as e:
                _logger.warning("[MCP Manager] Failed to list tools from %s: %s", name, e)
                result[name] = []
        return result

    @property
    def connected_servers(self) -> list[str]:
        """Get list of connected servers."""
        return list(self.clients.keys())
