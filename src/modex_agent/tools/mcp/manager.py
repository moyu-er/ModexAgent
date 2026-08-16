"""MCP Client Manager

Unified management of MCP connections, supporting stdio, sse, and streamable_http transports.
"""

import asyncio
import logging
from contextlib import AsyncExitStack, suppress
from typing import Any

from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import (
    _DEFAULT_TOOL_TIMEOUT,
    _STDIO_POLLUTION_MARKERS,
    BaseMCPClient,
)
from modex_agent.tools.mcp.connection import (
    MCPConnectionError,
    connect_single_server,
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


class MCPClientManager(McpBackend):
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
            client = await connect_single_server(
                name,
                server_config,
                injector=self._injector,
                stack=server_stack,
            )

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

        except MCPConnectionError as e:
            # Connect-level misconfiguration (no transport inferable, missing
            # command/url, unknown transport). Today this is a skip + warning,
            # not a hard failure — clean up the stack and continue.
            # Log-level consolidation: config-level errors (MCPConnectionError)
            # previously surfaced at ERROR via the BaseException branch below;
            # they now warn uniformly at WARNING as skippable misconfiguration.
            await self._safe_aclose(server_stack)
            _logger.warning("[MCP:%s] %s, skipping", name, e)
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
            if isinstance(e, KeyboardInterrupt | SystemExit):
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

    def _client_for(self, name: str) -> BaseMCPClient | None:
        """Return the client for ``name``, or ``None`` if not connected."""
        return self.clients.get(name)

    async def release(self) -> None:
        """Release all MCP connections (teardown contract from McpBackend)."""
        await self.disconnect_all()

    # -- invocation with reconnect-on-disconnect --------------------------------
    #
    # These override the ABC's pure-delegation defaults: when the first call
    # reports "not connected" (the client has dropped), the owning backend
    # reconnects once and retries the same call. Consumers (MCPTool, ...) thus
    # depend only on the McpBackend surface — they do not call
    # ``reconnect_with_retry`` directly, so a backend without reconnect (e.g.
    # the future SharedMcpBackend facade) remains a valid backend.

    async def execute_tool(
        self,
        server_name: str,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> dict[str, Any]:
        """Execute a tool, reconnecting once on a dropped connection."""
        result = await super().execute_tool(server_name, tool_name, params, timeout=timeout)
        if not result.get("success") and "not connected" in str(result.get("error", "")).lower():
            _logger.warning("[MCP:%s] connection dropped mid-call, reconnecting...", server_name)
            if await self.reconnect_with_retry(server_name):
                result = await super().execute_tool(server_name, tool_name, params, timeout=timeout)
        return result

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
