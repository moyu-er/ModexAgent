"""Tests for ``_load_agent_mcp_tools`` registry branch (ADR-0017 Task 5a).

When a :class:`McpConnectionRegistry` is passed, the loader must obtain a
:class:`SharedMcpBackend` via ``registry.acquire`` and adapt its tools — NOT
build a private ``MCPClientManager``. When ``mcp_registry=None`` (default),
today's per-pool path runs byte-for-byte (selection resolves to nothing when
no registry.json is present).
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

_BOT_PROJECT = Path(__file__).resolve().parents[3]
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from bot.service.builders import _load_agent_mcp_tools
from modex_agent.tools.mcp.client import BaseMCPClient
from modex_agent.tools.mcp.injector import MCPTransportInjector
from modex_agent.tools.mcp.manager import MCPClientManager
from modex_agent.tools.mcp.registry import (
    McpConnectionRegistry,
    SharedMcpBackend,
)


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

    async def list_resources(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return []

    async def list_prompts(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return []


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


# ── Registry branch ──


class TestLoadAgentMcpToolsRegistryBranch:
    @pytest.mark.asyncio
    async def test_registry_branch_returns_shared_backend(self, tmp_path: Path) -> None:
        """A real registry + stub connect yields a SharedMcpBackend + tools.

        No registry.json is needed on disk (the registry already holds the full
        server map); ``acquire`` logs+skips unknown names.
        """
        reg = McpConnectionRegistry(
            {"s1": {"transport": "stdio", "command": "echo", "args": ["hi"]}},
            connect_fn=_make_connect_fn(_ONE_TOOL),
        )
        try:
            tools, mcp_manager = await _load_agent_mcp_tools(
                "agent-a",
                ["s1"],
                tmp_path,
                mcp_registry=reg,
            )
            # The tool surfaces with the server name in it (default_prefix=True
            # → "mcp_<server>_<tool>"). Asserting on the server-token substring
            # rather than the exact prefix keeps this robust to prefix changes.
            assert len(tools) == 1
            assert "search" in tools[0].name
            assert "s1" in tools[0].name
            # The backend is a SharedMcpBackend, NOT a private MCPClientManager.
            assert isinstance(mcp_manager, SharedMcpBackend)
            assert not isinstance(mcp_manager, MCPClientManager)
            assert mcp_manager.connected_servers == ["s1"]
        finally:
            await reg.shutdown()

    @pytest.mark.asyncio
    async def test_registry_branch_empty_selection_returns_none(self, tmp_path: Path) -> None:
        """Empty selection short-circuits to ([], None) even with a registry."""
        reg = McpConnectionRegistry({}, connect_fn=_make_connect_fn())
        try:
            tools, mcp_manager = await _load_agent_mcp_tools(
                "agent-a", [], tmp_path, mcp_registry=reg,
            )
            assert tools == []
            assert mcp_manager is None
        finally:
            await reg.shutdown()

    @pytest.mark.asyncio
    async def test_registry_branch_acquire_failure_fails_soft(self, tmp_path: Path) -> None:
        """If acquire raises, the loader returns ([], None) — MCP never breaks the pool."""

        class _BrokenRegistry(McpConnectionRegistry):
            async def acquire(self, selection, *, timeout=8.0):  # type: ignore[override]
                raise RuntimeError("simulated acquire failure")

        reg = _BrokenRegistry({}, connect_fn=_make_connect_fn())
        try:
            tools, mcp_manager = await _load_agent_mcp_tools(
                "agent-a", ["s1"], tmp_path, mcp_registry=reg,
            )
            assert tools == []
            assert mcp_manager is None
        finally:
            await reg.shutdown()


# ── Non-registry branch (default None) — pins today's path unchanged ──


class TestLoadAgentMcpToolsNoneBranch:
    @pytest.mark.asyncio
    async def test_none_branch_no_registry_json_returns_empty(self, tmp_path: Path) -> None:
        """With mcp_registry=None and no registry.json, selection resolves to
        nothing and the loader returns ([], None) without raising.

        This pins that the default path is byte-for-byte unchanged: the flag-off
        / non-registry world behaves exactly as before.
        """
        tools, mcp_manager = await _load_agent_mcp_tools(
            "agent-a", ["s1"], tmp_path, mcp_registry=None,
        )
        assert tools == []
        assert mcp_manager is None

    @pytest.mark.asyncio
    async def test_none_branch_empty_selection(self, tmp_path: Path) -> None:
        """Empty selection short-circuits in both branches."""
        tools, mcp_manager = await _load_agent_mcp_tools(
            "agent-a", [], tmp_path, mcp_registry=None,
        )
        assert tools == []
        assert mcp_manager is None
