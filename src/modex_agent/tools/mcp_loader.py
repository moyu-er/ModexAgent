"""MCP loader for per-agent tool resolution."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.core.tool_manager import Tool, ToolManager
    from modex_agent.tools.mcp.backend import McpBackend
    from modex_agent.tools.mcp.registry import McpConnectionRegistry

logger = logging.getLogger(__name__)


async def load_per_agent_mcp(
    tool_manager: ToolManager,
    selection: list[str],
    project_dir: Path,
    agent_name: str,
    *,
    registry: McpConnectionRegistry | None = None,
    tool_transform: Callable[[Tool], Tool] | None = None,
) -> McpBackend | None:
    """Resolve an agent's MCP ``selection`` and register the adapted tools.

    Reads ``<project_dir>/config/mcp/registry.json`` (Claude-style
    ``{"mcpServers": {...}}``), keeps only the servers named in
    ``selection``, applies ``${ENV}`` interpolation, connects, and registers
    the adapted tools on ``tool_manager``. Failures are logged and swallowed
    so agent creation is never blocked by an unreachable MCP server.

    When ``registry`` is provided (ADR-0017 shared-connection overlay), the
    registry.json read and private ``MCPClientManager`` are bypassed: a
    :class:`SharedMcpBackend` is obtained via ``registry.acquire(selection)``
    instead. ``project_dir`` is then unused for MCP (it stays in the signature
    for the non-registry path).

    Returns the live :class:`McpBackend` (``SharedMcpBackend`` facade or
    private ``MCPClientManager``) so the caller can keep the connection
    lifecycle handle — ``release()`` detaches the facade (shared path) or
    closes the private connections. ``None`` when nothing was loaded.
    """
    import json

    from modex_agent.ioc.configs.app import _resolve_env_in
    from modex_agent.tools.mcp import MCPClientManager
    from modex_agent.tools.mcp_adapter import acquire_mcp_tools

    if not selection:
        return None

    if registry is not None:
        # Shared-connection path: the registry owns connection lifecycle and
        # already knows the server configs, so registry.json is not read.
        try:
            backend = await registry.acquire(selection)
        except Exception:
            logger.exception(
                "Agent %s: shared MCP acquire failed; continuing without MCP tools",
                agent_name,
            )
            return None

        tools = await acquire_mcp_tools(backend, tool_timeout=60)
        for tool in tools:
            tool_manager.register(tool_transform(tool) if tool_transform is not None else tool)

        logger.info(
            "Agent %s: %d MCP tools loaded from selection %s",
            agent_name,
            len(tools),
            selection,
        )
        return backend

    registry_path = project_dir / "config" / "mcp" / "registry.json"
    if not registry_path.exists():
        logger.info("Agent %s: MCP registry %s missing; skipping MCP tools", agent_name, registry_path)
        return None

    with open(registry_path, encoding="utf-8") as f:
        raw = json.load(f)

    # Fail-soft (framework) vs fail-loud (bot). The bot path
    # (``bot.config.mcp_registry.resolve_agent_mcp_servers``) raises
    # ``UnknownMcpServer`` for stale/typo'd selections at pool build; here we
    # only warn and drop unknown names so an agent still materializes — a
    # bad MCP selection must never block agent construction (the framework
    # runs under arbitrary business wiring, including stale YAML during tests).
    all_servers = raw.get("mcpServers") or raw.get("servers") or {}
    missing = [s for s in selection if s not in all_servers]
    if missing:
        logger.warning(
            "Agent %s: MCP servers not in registry %s: %s",
            agent_name, registry_path.name, missing,
        )
        return None

    servers = {name: all_servers[name] for name in selection}
    logger.info(
        "Agent %s: loading MCP from %s — %d server(s): %s",
        agent_name, registry_path.name, len(servers), list(servers.keys()),
    )
    servers = _resolve_env_in(servers)
    manager = MCPClientManager(config=servers)

    # Wrap with a hard timeout so unreachable servers never block
    # agent creation. httpx has its own timeout, but DNS / TCP
    # handshake can still hang on some platforms.
    import asyncio as _asyncio

    try:
        await _asyncio.wait_for(manager.initialize(), timeout=15.0)
    except TimeoutError:
        logger.warning(
            "Agent %s: MCP initialization timed out after 15s for %s — "
            "server(s) %s unreachable; continuing without MCP tools",
            agent_name, registry_path.name, list(servers.keys()),
        )
        return None
    except Exception:
        logger.exception(
            "Agent %s: MCP initialization failed for %s",
            agent_name, registry_path.name,
        )
        return None

    if not manager.connected_servers:
        logger.warning(
            "Agent %s: MCP config %s — %d server(s) configured but NONE connected "
            "(check MCP server credentials/env vars and network)",
            agent_name, registry_path.name, len(servers),
        )
        return None

    adapter_tools = await acquire_mcp_tools(manager, tool_timeout=60)
    for tool in adapter_tools:
        tool_manager.register(tool_transform(tool) if tool_transform is not None else tool)

    logger.info(
        "Agent %s: %d MCP tools loaded from selection %s",
        agent_name,
        len(adapter_tools),
        selection,
    )
    return manager
