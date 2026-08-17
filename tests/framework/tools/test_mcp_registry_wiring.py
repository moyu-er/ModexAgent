"""Tests for the opt-in shared-registry branch at the two MCP call sites.

Per ADR-0017 Task 4. ``connect_mcp`` (ioc.factories.tools) and
``load_per_agent_mcp`` (tools.mcp_loader) gain an optional ``registry``
keyword. When set, they obtain a :class:`SharedMcpBackend` via
``registry.acquire(selection)`` instead of building a private ``MCPClientManager``.
When ``registry=None`` (default), today's behavior is byte-for-byte unchanged.

These tests exercise the REAL :class:`McpConnectionRegistry` (Task 3) driven by
a fake ``connect_fn`` (the test seam), so the real acquire/supervisor path runs.
No real subprocesses, no network — the ``connect_fn`` returns a stub client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from modex_agent.ioc.configs.mcp import MCPConfig, MCPServerEntry
from modex_agent.ioc.factories.tools import connect_mcp
from modex_agent.tools.mcp.client import BaseMCPClient
from modex_agent.tools.mcp.injector import MCPTransportInjector
from modex_agent.tools.mcp.registry import (
    McpConnectionRegistry,
    SharedMcpBackend,
)
from modex_agent.tools.mcp_loader import load_per_agent_mcp


class _StubClient(BaseMCPClient):
    """Minimal ``BaseMCPClient`` for in-process registry tests.

    Overrides ``list_tools`` to return a configurable tool list so the adapter
    can surface a prefixed tool. The registry's supervisor calls the connect_fn
    which returns this stub; the stub's ``initialize`` is a no-op (the
    connect_fn contract returns an already-initialized client).
    """

    def __init__(
        self,
        name: str = "stub",
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:  # noqa: D401 - test stub
        super().__init__(name=name)
        self._tools = tools if tools is not None else []
        # Mark initialized so McpBackend default delegation (which some paths
        # consult via session presence) sees a live client.
        self._initialized = True

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return list(self._tools)


def _make_connect_fn(
    tools: list[dict[str, Any]] | None = None,
    *,
    fail_for: set[str] | None = None,
) -> Callable[..., Awaitable[BaseMCPClient]]:
    """Build a fake ``connect_fn`` returning a stub client per server name.

    ``fail_for`` names servers whose connect should raise (drives the FAILED
    terminal state). Matches the real ``connect_single_server`` signature:
    ``connect_fn(name, server_config, *, injector, stack)``.
    """

    async def _connect(
        name: str,
        server_config: dict[str, Any],
        *,
        injector: MCPTransportInjector,
        stack: AsyncExitStack,
    ) -> BaseMCPClient:
        if fail_for and name in fail_for:
            raise RuntimeError(f"simulated connect failure for {name}")
        return _StubClient(name=name, tools=tools)

    return _connect


def _stdio_mcp_config() -> MCPConfig:
    """A minimal MCPConfig with one stdio server (never really spawned)."""
    return MCPConfig(
        servers={
            "s1": MCPServerEntry(
                transport="stdio",
                command="echo",
                args=["hi"],
            ),
        },
    )


# --------------------------------------------------------------------------- #
# connect_mcp (ioc.factories.tools)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_connect_mcp_uses_registry_when_provided() -> None:
    """When a registry is passed, the adapter wraps a SharedMcpBackend."""
    from modex_agent.tools.mcp import MCPClientManager

    reg = McpConnectionRegistry(
        {"s1": {"transport": "stdio", "command": "echo", "args": ["hi"]}},
        connect_fn=_make_connect_fn(),
    )
    try:
        adapter = await connect_mcp(_stdio_mcp_config(), registry=reg)
        assert adapter is not None
        # The registry branch must yield a SharedMcpBackend, NOT a MCPClientManager.
        assert isinstance(adapter.mcp_manager, SharedMcpBackend)
        assert not isinstance(adapter.mcp_manager, MCPClientManager)
        assert adapter.mcp_manager.connected_servers == ["s1"]
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_connect_mcp_falls_back_without_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With registry=None, the default MCPClientManager path runs unchanged."""
    constructed: list[Any] = []

    class _FakeManager:
        def __init__(self, config: dict[str, Any], injector: object = None) -> None:
            del config, injector
            constructed.append(self)
            self.connected_servers: list[str] = []

        async def initialize(self) -> None:
            pass

        async def release(self) -> None:
            pass

    # connect_mcp imports MCPClientManager lazily from modex_agent.tools.mcp.
    import modex_agent.tools.mcp as mcp_mod

    monkeypatch.setattr(mcp_mod, "MCPClientManager", _FakeManager)

    adapter = await connect_mcp(_stdio_mcp_config())
    assert adapter is not None
    assert constructed, "the non-registry branch must construct a MCPClientManager"
    assert adapter.mcp_manager is constructed[0]


@pytest.mark.asyncio
async def test_connect_mcp_none_config_returns_none() -> None:
    """None config early-returns None on both paths."""
    reg = McpConnectionRegistry({}, connect_fn=_make_connect_fn())
    try:
        assert await connect_mcp(None, registry=reg) is None
        assert await connect_mcp(None) is None
    finally:
        await reg.shutdown()


# --------------------------------------------------------------------------- #
# load_per_agent_mcp (tools.mcp_loader)
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_load_per_agent_mcp_uses_registry_when_provided(tmp_path: Path) -> None:
    """Registry branch registers prefixed tools WITHOUT reading registry.json."""
    from modex_agent.core.tool_manager import InMemoryToolManager

    one_tool = [{"name": "t1", "description": "d", "inputSchema": {"type": "object"}}]
    reg = McpConnectionRegistry(
        {"s1": {"transport": "stdio", "command": "echo"}},
        connect_fn=_make_connect_fn(tools=one_tool),
    )
    try:
        tm = InMemoryToolManager()
        # tmp_path has NO config/mcp/registry.json — the registry branch must
        # not touch it, proving registry.json was bypassed.
        await load_per_agent_mcp(
            tm, ["s1"], project_dir=tmp_path, agent_name="a", registry=reg,
        )
        # tool name = {server}_{tool}
        assert "s1_t1" in tm.list_tools()
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_load_per_agent_mcp_registry_acquire_failure_is_fail_soft(
    tmp_path: Path,
) -> None:
    """A FAILED connect must not raise and must register zero tools."""
    from modex_agent.core.tool_manager import InMemoryToolManager

    reg = McpConnectionRegistry(
        {"s1": {"transport": "stdio", "command": "echo"}},
        connect_fn=_make_connect_fn(fail_for={"s1"}),
    )
    try:
        tm = InMemoryToolManager()
        # Must not raise; the selected server fails to connect → no tools.
        await load_per_agent_mcp(
            tm, ["s1"], project_dir=tmp_path, agent_name="a", registry=reg,
        )
        assert tm.list_tools() == []
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_load_per_agent_mcp_falls_back_without_registry(tmp_path: Path) -> None:
    """With registry=None and no registry.json, the default path logs+returns."""
    from modex_agent.core.tool_manager import InMemoryToolManager

    tm = InMemoryToolManager()
    # tmp_path has no config/mcp/registry.json → early "missing; skipping" return.
    await load_per_agent_mcp(
        tm, ["s1"], project_dir=tmp_path, agent_name="a",
    )
    assert tm.list_tools() == []
