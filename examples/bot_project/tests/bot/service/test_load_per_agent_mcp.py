"""Tests for the FW per-agent MCP loader's registry branch (ADR-0017 5a).

Ticket 10: the BIZ ``_load_agent_mcp_tools`` helper is deleted — per-agent
MCP loading (main agents at Stage 4, subagents at materialization) runs
through :func:`modex_agent.tools.mcp_loader.load_per_agent_mcp` reading the
shared-connection handle from the context chain. These tests re-pin the
ADR-0017 Task 5a contract at that seam, including the ticket-10 backend
return (the caller keeps the connection-lifecycle handle).
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Never

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from modex_agent.tools.mcp.client import BaseMCPClient
from modex_agent.tools.mcp.injector import MCPTransportInjector
from modex_agent.tools.mcp.manager import MCPClientManager
from modex_agent.tools.mcp.registry import (
    McpConnectionRegistry,
    SharedMcpBackend,
)
from modex_agent.tools.mcp_loader import load_per_agent_mcp
from modex_agent.tools.manager import InMemoryToolManager

# ── Stub client + fake connect_fn (mirrors the framework registry tests) ──


class _StubClient(BaseMCPClient):
    """Minimal ``BaseMCPClient`` advertising a configurable tool list.

    The registry's supervisor calls ``connect_fn`` which returns this stub; the
    stub's ``initialize`` is a no-op (the connect_fn contract returns an
    already-initialized client).
    """

    def __init__(
        self,
        name: str = "stub",
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:  # noqa: D401 - test stub
        super().__init__(name=name)
        self._tools = tools if tools is not None else []
        self._initialized = True

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return list(self._tools)


def _make_connect_fn(
    tools: list[dict[str, Any]] | None = None,
) -> Callable[..., Awaitable[BaseMCPClient]]:
    """Build a fake ``connect_fn`` returning a stub client per server name.

    Matches the real ``connect_single_server`` signature:
    ``connect_fn(name, server_config, *, injector, stack)``.
    """

    async def _connect(
        name: str,
        server_config: dict[str, Any],
        *,
        injector: MCPTransportInjector,
        stack: AsyncExitStack,
    ) -> BaseMCPClient:
        return _StubClient(name=name, tools=tools)

    return _connect


_ONE_TOOL = [{"name": "search", "description": "stub tool", "inputSchema": {}}]


def _tool_manager() -> InMemoryToolManager:
    return InMemoryToolManager()


# ── Registry branch ──


class TestLoadPerAgentMcpRegistryBranch:
    @pytest.mark.asyncio
    async def test_registry_branch_returns_shared_backend(self, tmp_path: Path) -> None:
        """A real registry + stub connect yields a SharedMcpBackend + tools
        registered on the tool manager (the caller keeps the backend)."""
        reg = McpConnectionRegistry(
            {"s1": {"transport": "stdio", "command": "echo", "args": ["hi"]}},
            connect_fn=_make_connect_fn(_ONE_TOOL),
        )
        try:
            tm = _tool_manager()
            backend = await load_per_agent_mcp(
                tm, ["s1"], tmp_path, "agent-a", registry=reg
            )
            # Tool name = {server}_{tool} (opencode convention).
            # Asserting on the server-token substring keeps this robust.
            tool_names = tm.list_tools()
            assert len(tool_names) == 1
            assert "search" in tool_names[0]
            assert "s1" in tool_names[0]
            # The backend is a SharedMcpBackend, NOT a private MCPClientManager.
            assert isinstance(backend, SharedMcpBackend)
            assert not isinstance(backend, MCPClientManager)
            assert backend.connected_servers == ["s1"]
        finally:
            await reg.shutdown()

    @pytest.mark.asyncio
    async def test_registry_branch_empty_selection_returns_none(self, tmp_path: Path) -> None:
        """Empty selection short-circuits to ``None`` even with a registry."""
        reg = McpConnectionRegistry({}, connect_fn=_make_connect_fn())
        try:
            backend = await load_per_agent_mcp(
                _tool_manager(), [], tmp_path, "agent-a", registry=reg
            )
            assert backend is None
        finally:
            await reg.shutdown()

    @pytest.mark.asyncio
    async def test_registry_branch_acquire_failure_fails_soft(self, tmp_path: Path) -> None:
        """If acquire raises, the loader returns ``None`` — MCP never breaks
        the agent assembly."""

        class _BrokenRegistry(McpConnectionRegistry):
            async def acquire(self, selection, *, timeout=8.0) -> Never:  # type: ignore[override]
                raise RuntimeError("simulated acquire failure")

        reg = _BrokenRegistry({}, connect_fn=_make_connect_fn())
        try:
            backend = await load_per_agent_mcp(
                _tool_manager(), ["s1"], tmp_path, "agent-a", registry=reg
            )
            assert backend is None
        finally:
            await reg.shutdown()


# ── Non-registry branch (default None) — pins the flag-off path ──


class TestLoadPerAgentMcpNoneBranch:
    @pytest.mark.asyncio
    async def test_none_branch_no_registry_json_returns_none(self, tmp_path: Path) -> None:
        """With registry=None and no registry.json, the selection resolves to
        nothing and the loader returns ``None`` without raising."""
        backend = await load_per_agent_mcp(
            _tool_manager(), ["s1"], tmp_path, "agent-a", registry=None
        )
        assert backend is None

    @pytest.mark.asyncio
    async def test_none_branch_empty_selection(self, tmp_path: Path) -> None:
        """Empty selection short-circuits in both branches."""
        backend = await load_per_agent_mcp(
            _tool_manager(), [], tmp_path, "agent-a", registry=None
        )
        assert backend is None

    @pytest.mark.asyncio
    async def test_none_branch_unknown_server_name_fails_soft(self, tmp_path: Path) -> None:
        """A registry.json naming different servers: the unknown selection is
        dropped with a warning — never a hard failure (the fail-soft
        contract the BIZ loader's loud branch used to carry on the bot
        side; the FW seam keeps assembly unblocked)."""
        registry_json = tmp_path / "config" / "mcp" / "registry.json"
        registry_json.parent.mkdir(parents=True)
        registry_json.write_text(
            '{"mcpServers": {"other": {"command": "echo"}}}', encoding="utf-8"
        )
        tm = _tool_manager()
        backend = await load_per_agent_mcp(
            tm, ["missing-server"], tmp_path, "agent-a", registry=None
        )
        assert backend is None
        assert tm.list_tools() == []
