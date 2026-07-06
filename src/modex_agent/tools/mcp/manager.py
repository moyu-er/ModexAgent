"""MCP Client Manager

Unified management of MCP connections, supporting stdio, sse, and streamable_http transports.
"""

import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from typing import Any

from mcp import ClientSession

from modex_agent.tools.mcp.client import (
    _DEFAULT_TOOL_TIMEOUT,
    _STDIO_POLLUTION_MARKERS,
    _TRANSPORT_ALIASES,
    BaseMCPClient,
    SSEMCPClient,
    StdioMCPClient,
    StreamableHttpMCPClient,
    TransportType,
)
from modex_agent.tools.mcp.injector import (
    JsonFileMCPTransportInjector,
    MCPTransportInjector,
)

_logger = logging.getLogger(__name__)

# Per-server connect timeout. Bounds startup when an MCP server is unreachable
# or rejects auth (e.g. streamable_http 401 makes the SDK block on the response
# stream until the underlying httpx read timeout — 60s — which is too long for
# an eager startup init). A failing server is skipped after this many seconds.
_CONNECT_TIMEOUT: float = 20.0


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

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        injector: MCPTransportInjector | None = None,
    ) -> None:
        """Initialize MCP client manager.

        Args:
            config: Custom config dict, format:
                {server_name: {transport, command, args, url, headers, env, enabled, tool_timeout, enabled_tools, ...}}
                If None, uses empty config (servers must be provided explicitly).
            injector: Optional runtime injector for env/headers. If None, a
                JSON-file injector pointing at ``.modex/mcp_inject.json`` is used.
        """
        self.clients: dict[str, BaseMCPClient] = {}
        self._server_stacks: dict[str, AsyncExitStack] = {}
        self._initialized = False
        self._custom_config = config or {}
        self._injector = injector if injector is not None else JsonFileMCPTransportInjector()
        self._reconnect_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize all configured MCP servers."""
        if self._initialized:
            return

        _logger.info("[MCP Manager] Initializing MCP servers...")

        servers = self._custom_config.items()

        for name, server_config in servers:
            enabled = (
                server_config.get("enabled", True) if isinstance(server_config, dict) else True
            )

            if not enabled:
                _logger.info("[MCP Manager] Skipping disabled server: %s", name)
                continue

            try:
                result = await asyncio.wait_for(
                    self._connect_single(name, server_config),
                    timeout=_CONNECT_TIMEOUT,
                )
            except TimeoutError:
                _logger.warning(
                    "[MCP Manager] %s connect timed out after %.0fs — skipping",
                    name,
                    _CONNECT_TIMEOUT,
                )
                continue
            except asyncio.CancelledError:
                # Whole init cancelled (e.g. service shutdown) — propagate without
                # touching per-server state; partial stacks were already cleaned
                # in-task by _connect_single.
                raise
            except Exception as e:
                _logger.error("[MCP Manager] Failed to connect to %s: %s", name, e)
                continue

            if result is not None and result[1] is not None:
                self.clients[result[0]] = result[1]

        self._initialized = True
        _logger.info("[MCP Manager] Initialized %d MCP servers", len(self.clients))

    async def _connect_single(
        self, name: str, server_config: dict[str, Any]
    ) -> tuple[str, BaseMCPClient | None] | None:
        """Connect to a single MCP server."""
        server_stack = AsyncExitStack()
        await server_stack.__aenter__()

        try:
            # Normalize raw dict input: "environment" alias → "env"
            if "environment" in server_config and "env" not in server_config:
                server_config = {**server_config, "env": server_config["environment"]}

            # Normalize "type" → "transport" (Claude mcp.json convention)
            raw_type = server_config.get("type") or server_config.get("transport", "")
            transport = raw_type.lower() if isinstance(raw_type, str) else ""

            # "local" → stdio
            if transport == "local":
                transport = TransportType.STDIO

            if not transport:
                cmd = server_config.get("command")
                if cmd and (isinstance(cmd, str) and cmd.strip() or isinstance(cmd, list) and cmd):
                    transport = TransportType.STDIO
                elif server_config.get("url"):
                    url = server_config["url"]
                    transport = (
                        TransportType.SSE
                        if url.rstrip("/").endswith("/sse")
                        else TransportType.STREAMABLE_HTTP
                    )
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

        except BaseException as e:
            # Connect failed OR was cancelled. ``except Exception`` alone is
            # insufficient: a streamable_http 401 makes the SDK tear down its
            # anyio TaskGroup, which surfaces here as a ``CancelledError``
            # ("Cancelled by cancel scope") — a ``BaseException`` the general
            # except would let escape, leaking the half-entered ``server_stack``
            # so the transport async generator is later GC-closed in a
            # different task, raising ``RuntimeError: Attempted to exit cancel
            # scope in a different task than it was entered in``.
            #
            # MCP connect is best-effort: clean up the stack IN THIS TASK
            # (preventing the cross-task GC teardown) and skip the server for
            # every non-fatal cause. Propagate only true interpreter signals.
            await self._safe_aclose(server_stack)
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise

            hint = ""
            text = str(e).lower()
            if any(marker in text for marker in _STDIO_POLLUTION_MARKERS):
                hint = (
                    " Hint: this looks like stdio protocol pollution. Make sure the MCP server writes "
                    "only JSON-RPC to stdout and sends logs/debug output to stderr instead."
                )

            # Extract root cause from ExceptionGroup (Python 3.11+) — a 401
            # arrives wrapped in a TaskGroup BaseExceptionGroup.
            root_cause: BaseException = e
            if isinstance(e, BaseExceptionGroup):
                sub_exceptions = list(e.exceptions)
                if sub_exceptions:
                    root_cause = sub_exceptions[0]

            if isinstance(e, asyncio.CancelledError):
                _logger.warning(
                    "[MCP:%s] connect cancelled (%s) — skipping%s",
                    name, type(root_cause).__name__, hint,
                )
            else:
                _logger.error(
                    "[MCP:%s] Failed to connect: %s%s",
                    name, root_cause, hint,
                )
            _logger.debug(
                "[MCP:%s] Full exception traceback:",
                name,
                exc_info=True,
            )
            return name, None

    @staticmethod
    async def _safe_aclose(stack: AsyncExitStack) -> None:
        """Close an AsyncExitStack, swallowing cleanup errors.

        Cleanup failures (including the anyio cross-task ``RuntimeError`` that
        surfaces when an MCP transport's TaskGroup is torn down) must not escape
        — they are non-fatal during connect failure / shutdown.
        """
        with suppress(BaseException):
            await stack.aclose()

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

        raw_command = server_config.get("command")
        if not raw_command:
            raise MCPConnectionError("Command required for stdio transport")

        # Support command as list: ["npx", "-y", "@playwright/mcp"]
        # First element → command, rest → prepend to args
        if isinstance(raw_command, list):
            if not raw_command:
                raise MCPConnectionError("Command list must not be empty")
            command = str(raw_command[0])
            extra_args = [str(a) for a in raw_command[1:]]
            args = extra_args + server_config.get("args", [])
        else:
            command = str(raw_command)
            args = server_config.get("args", [])

        env = server_config.get("env", {})
        env, _ = self._injector.apply(name, str(TransportType.STDIO), env, {})

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
        _, config_headers = self._injector.apply(
            name, str(TransportType.SSE), {}, config_headers
        )

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
        _, headers = self._injector.apply(
            name, str(TransportType.STREAMABLE_HTTP), {}, headers
        )

        http_client = await server_stack.enter_async_context(
            httpx.AsyncClient(
                headers=headers or None,
                follow_redirects=True,
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0),
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
                delay = min(base_delay * (2**attempt), max_delay)
                _logger.info(
                    "[MCP Manager] Retrying %s in %.1fs (attempt %d/%d)",
                    server_name,
                    delay,
                    attempt + 1,
                    max_retries,
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
                    _logger.warning(
                        "[MCP Manager] No config for server '%s', cannot reconnect", server_name
                    )
                    return False
                await self.disconnect(server_name)
                result = await self._connect_single(server_name, config)
                if result is not None and result[1] is not None:
                    self.clients[result[0]] = result[1]
                    _logger.info("[MCP Manager] Reconnected to %s", server_name)
                    return True
                return False

            success = False
            for name, server_config in list(self._custom_config.items()):
                await self.disconnect(name)
                result = await self._connect_single(name, server_config)
                if result is not None and result[1] is not None:
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
            return {"success": False, "error": f"MCP server not connected: {server_name}"}

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
            return {"success": False, "error": f"MCP server not connected: {server_name}"}

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
            return {"success": False, "error": f"MCP server not connected: {server_name}"}

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
