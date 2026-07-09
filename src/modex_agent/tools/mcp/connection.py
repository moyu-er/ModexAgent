"""Per-server MCP connect pipeline.

Extracted from :class:`modex_agent.tools.mcp.manager.MCPClientManager` (ADR-0017
T2). This module exposes a single module-level coroutine,
:func:`connect_single_server`, that performs transport normalization, transport
detection, and client creation (stdio / sse / streamable_http) for one MCP
server. It is the reusable primitive a connection supervisor (e.g. the future
:class:`McpConnectionRegistry`) can call without depending on
``MCPClientManager``'s private methods.

The function takes the caller-owned :class:`contextlib.AsyncExitStack` and the
:class:`MCPTransportInjector` to apply; it returns a connected
:class:`BaseMCPClient` (session initialized). Stack bookkeeping,
``list_tools`` logging, and error-hint extraction remain the caller's
responsibility (orchestration), so this module's behavior is byte-equivalent to
the previously inlined connect logic.
"""

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession

from modex_agent.tools.mcp.client import (
    _TRANSPORT_ALIASES,
    BaseMCPClient,
    SSEMCPClient,
    StdioMCPClient,
    StreamableHttpMCPClient,
    TransportType,
)
from modex_agent.tools.mcp.injector import MCPTransportInjector

__all__ = ["connect_single_server", "MCPConnectionError"]


class MCPConnectionError(Exception):
    """MCP connection error (e.g. unknown transport, missing command/url)."""

    pass


async def connect_single_server(
    name: str,
    server_config: dict[str, Any],
    *,
    injector: MCPTransportInjector,
    stack: AsyncExitStack,
) -> BaseMCPClient:
    """Connect a single MCP server and return its initialized client.

    Performs transport normalization and detection, then builds the matching
    :class:`BaseMCPClient` (stdio / sse / streamable_http) with its session
    initialized. The caller owns ``stack`` and is responsible for closing it
    (on failure or teardown) and for any post-connect orchestration such as
    ``list_tools`` logging.

    Args:
        name: Server name (used for injector scoping and client identity).
        server_config: Raw per-server config dict (open MCP config shape).
        injector: Transport injector applied to env/headers.
        stack: Caller-owned :class:`AsyncExitStack` that the transport, http
            client, and ``ClientSession`` are entered into.

    Returns:
        The connected :class:`BaseMCPClient` with ``session`` initialized and
        ``_initialized`` / ``_managed_externally`` set.

    Raises:
        MCPConnectionError: If no transport can be inferred (no command or
            url configured), or if a required field (command / url) is missing
            for the resolved transport.
    """
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
            raise MCPConnectionError("No command or url configured")

    transport = _TRANSPORT_ALIASES.get(transport, transport)

    if transport == TransportType.STDIO:
        return await _create_stdio_client(name, server_config, injector, stack)
    elif transport == TransportType.SSE:
        return await _create_sse_client(name, server_config, injector, stack)
    elif transport == TransportType.STREAMABLE_HTTP:
        return await _create_streamable_http_client(name, server_config, injector, stack)
    else:
        raise MCPConnectionError(f"Unknown transport: {transport}")


async def _create_stdio_client(
    name: str,
    server_config: dict[str, Any],
    injector: MCPTransportInjector,
    stack: AsyncExitStack,
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
    env, _ = injector.apply(name, str(TransportType.STDIO), env, {})

    params = StdioServerParameters(
        command=command,
        args=args or [],
        env=env or None,
    )

    read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    client = StdioMCPClient(name=name, command=command, args=args, env=env)
    client.session = session
    client._initialized = True
    client._managed_externally = True
    return client


async def _create_sse_client(
    name: str,
    server_config: dict[str, Any],
    injector: MCPTransportInjector,
    stack: AsyncExitStack,
) -> BaseMCPClient:
    """Create SSE MCP client."""
    import httpx
    from mcp.client.sse import sse_client

    url = server_config.get("url")
    if not url:
        raise MCPConnectionError("URL required for sse transport")

    config_headers = server_config.get("headers", {})
    _, config_headers = injector.apply(
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

    sse_transport = await stack.enter_async_context(
        sse_client(url, httpx_client_factory=httpx_client_factory)
    )
    session = await stack.enter_async_context(
        ClientSession(sse_transport[0], sse_transport[1])
    )
    await session.initialize()

    client = SSEMCPClient(name=name, url=url, headers=config_headers)
    client.session = session
    client._initialized = True
    client._managed_externally = True
    return client


async def _create_streamable_http_client(
    name: str,
    server_config: dict[str, Any],
    injector: MCPTransportInjector,
    stack: AsyncExitStack,
) -> BaseMCPClient:
    """Create Streamable HTTP MCP client."""
    import httpx
    from mcp.client.streamable_http import streamable_http_client

    url = server_config.get("url")
    if not url:
        raise MCPConnectionError("URL required for http transport")

    headers = server_config.get("headers", {})
    _, headers = injector.apply(
        name, str(TransportType.STREAMABLE_HTTP), {}, headers
    )

    http_client = await stack.enter_async_context(
        httpx.AsyncClient(
            headers=headers or None,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=5.0),
        )
    )
    read, write, _ = await stack.enter_async_context(
        streamable_http_client(url, http_client=http_client)
    )
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    client = StreamableHttpMCPClient(name=name, url=url, headers=headers)
    client.session = session
    client._initialized = True
    client._managed_externally = True
    return client
