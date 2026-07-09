"""Reconnect-on-disconnect now lives in the owning backend, not the Tool wrapper.

ADR-0017 Task 6: the per-call reconnect dance (``not connected`` →
``reconnect_with_retry`` → retry once) was relocated from ``MCPTool`` /
``MCPResourceTool`` / ``MCPPromptTool`` into ``MCPClientManager``'s overrides
of ``execute_tool`` / ``read_resource`` / ``get_prompt``. Consumers therefore
depend only on the ``McpBackend`` surface and stay valid against a backend
that owns no reconnect (e.g. the future ``SharedMcpBackend`` facade).
"""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import BaseMCPClient
from modex_agent.tools.mcp.manager import MCPClientManager
from modex_agent.tools.mcp.tool import MCPPromptTool, MCPResourceTool, MCPTool


class _ScriptedClient(BaseMCPClient):
    """``BaseMCPClient`` stand-in returning a scripted sequence of results.

    Each public method pops its next canned response off a list, recording the
    call, so a test can assert exactly how many calls happened (one before the
    reconnect, one on the retry).
    """

    def __init__(  # noqa: D401 - test stub
        self,
        tool_results: list[dict[str, Any]] | None = None,
        resource_results: list[dict[str, Any]] | None = None,
        prompt_results: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(name="scripted")
        self.tool_results = list(tool_results or [])
        self.resource_results = list(resource_results or [])
        self.prompt_results = list(prompt_results or [])
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def call_tool(  # type: ignore[override]
        self,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(("call_tool", (tool_name, params), {"timeout": timeout}))
        return self.tool_results.pop(0)

    async def read_resource(  # type: ignore[override]
        self,
        uri: str,
        timeout: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(("read_resource", (uri,), {"timeout": timeout}))
        return self.resource_results.pop(0)

    async def get_prompt(  # type: ignore[override]
        self,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_prompt", (prompt_name,), {"arguments": arguments, "timeout": timeout})
        )
        return self.prompt_results.pop(0)


def _manager_with_client(client: _ScriptedClient) -> MCPClientManager:
    """Build a manager pre-populated with a single connected client (no init)."""
    manager = MCPClientManager()
    manager.clients["s1"] = client
    return manager


# ---------------------------------------------------------------------------
# 1. MCPClientManager.execute_tool / read_resource / get_prompt: reconnect-once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_tool_reconnects_and_retries_once_on_not_connected() -> None:
    """First call reports not-connected → reconnect succeeds → retry succeeds.

    The reconnect dance must live in the manager override. The dropped-client
    path is realistic: ``_client_for`` misses on the first call (the ABC default
    returns the not-connected error), ``reconnect_with_retry`` repopulates the
    client, and the single retry routes through the new client and succeeds.
    """
    # Fresh client bound only AFTER reconnect — the pre-reconnect client was
    # dropped, so it never gets called.
    reconnected_client = _ScriptedClient(tool_results=[{"success": True, "result": "ok"}])
    manager = MCPClientManager()  # no clients bound — simulates a dropped connection
    reconnect_calls: list[str] = []

    async def fake_reconnect(server_name: str, **_: Any) -> bool:
        reconnect_calls.append(server_name)
        manager.clients["s1"] = reconnected_client  # repopulate, as a real reconnect would
        return True

    manager.reconnect_with_retry = fake_reconnect  # type: ignore[assignment]

    result = await manager.execute_tool("s1", "sum", {"a": 1}, timeout=11)

    assert result == {"success": True, "result": "ok"}
    assert reconnect_calls == ["s1"]
    # Exactly one client ``call_tool`` — the single retry (the pre-reconnect
    # call short-circuited in the ABC default with no client bound).
    assert len(reconnected_client.calls) == 1
    _, args, kwargs = reconnected_client.calls[0]
    assert args == ("sum", {"a": 1})
    # Retry forwarded the SAME timeout as the first call.
    assert kwargs == {"timeout": 11}


@pytest.mark.asyncio
async def test_execute_tool_no_retry_when_reconnect_fails() -> None:
    """If reconnect fails, the original not-connected result is returned (no retry)."""
    manager = MCPClientManager()  # no clients bound

    async def fake_reconnect(server_name: str, **_: Any) -> bool:
        return False

    manager.reconnect_with_retry = fake_reconnect  # type: ignore[assignment]

    result = await manager.execute_tool("s1", "sum", {})

    assert result == {"success": False, "error": "MCP server not connected: s1"}
    # No client was ever bound, so no retry could happen — reconnect returned False.


@pytest.mark.asyncio
async def test_execute_tool_no_reconnect_when_error_is_unrelated() -> None:
    """A non-connection error (e.g. tool raised) must NOT trigger reconnect."""
    client = _ScriptedClient(tool_results=[{"success": False, "error": "division by zero"}])
    manager = _manager_with_client(client)

    reconnected = False

    async def fake_reconnect(server_name: str, **_: Any) -> bool:
        nonlocal reconnected
        reconnected = True
        return True

    manager.reconnect_with_retry = fake_reconnect  # type: ignore[assignment]

    result = await manager.execute_tool("s1", "sum", {})

    assert result == {"success": False, "error": "division by zero"}
    assert reconnected is False
    # Exactly one call — no retry.
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_read_resource_reconnects_and_retries_once() -> None:
    reconnected_client = _ScriptedClient(resource_results=[{"success": True, "result": "data"}])
    manager = MCPClientManager()  # no clients bound — dropped connection

    async def fake_reconnect(server_name: str, **_: Any) -> bool:
        manager.clients["s1"] = reconnected_client
        return True

    manager.reconnect_with_retry = fake_reconnect  # type: ignore[assignment]

    result = await manager.read_resource("s1", "u://x", timeout=7)

    assert result == {"success": True, "result": "data"}
    assert len(reconnected_client.calls) == 1
    _, args, kwargs = reconnected_client.calls[0]
    assert args == ("u://x",)
    assert kwargs == {"timeout": 7}


@pytest.mark.asyncio
async def test_get_prompt_reconnects_and_retries_once_with_arguments() -> None:
    reconnected_client = _ScriptedClient(prompt_results=[{"success": True, "result": "filled"}])
    manager = MCPClientManager()  # no clients bound — dropped connection

    async def fake_reconnect(server_name: str, **_: Any) -> bool:
        manager.clients["s1"] = reconnected_client
        return True

    manager.reconnect_with_retry = fake_reconnect  # type: ignore[assignment]

    result = await manager.get_prompt("s1", "intro", arguments={"k": "v"}, timeout=9)

    assert result == {"success": True, "result": "filled"}
    assert len(reconnected_client.calls) == 1
    _, args, kwargs = reconnected_client.calls[0]
    assert args == ("intro",)
    assert kwargs == {"arguments": {"k": "v"}, "timeout": 9}


# ---------------------------------------------------------------------------
# 2. Tool wrappers: no reconnect reference; facade-compatible (pure McpBackend)
# ---------------------------------------------------------------------------


class _PureBackend(McpBackend):
    """A minimal ``McpBackend`` with NO ``reconnect_with_retry`` method.

    Proves the wrappers depend only on the ABC surface: a backend that owns no
    connection health (the shape the future ``SharedMcpBackend`` facade will
    have) is a valid ``mcp_manager`` for every wrapper.
    """

    def __init__(self, client: _ScriptedClient) -> None:  # noqa: D401 - test fake
        self._client = client

    @property
    def connected_servers(self) -> list[str]:
        return ["s1"]

    def _client_for(self, name: str) -> BaseMCPClient | None:
        return self._client if name == "s1" else None

    async def release(self) -> None:
        pass


@pytest.mark.asyncio
async def test_mcp_tool_works_against_pure_mcp_backend_no_reconnect() -> None:
    """MCPTool succeeds against a backend that has no reconnect_with_retry."""
    client = _ScriptedClient(tool_results=[{"success": True, "result": "called"}])
    backend = _PureBackend(client)

    tool = MCPTool(
        server_name="s1",
        tool_name="sum",
        description="sums",
        parameters={"type": "object", "properties": {}, "required": []},
        mcp_manager=backend,
    )

    out = await tool.execute(a=1)
    assert out == "called"
    # Exactly one client call — no reconnect dance attempted.
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_mcp_tool_formats_failure_string_byte_for_byte() -> None:
    """The wrapper's failure format is preserved after the reconnect relocation."""
    client = _ScriptedClient(tool_results=[{"success": False, "error": "boom"}])
    backend = _PureBackend(client)

    tool = MCPTool(
        server_name="s1",
        tool_name="sum",
        description="sums",
        parameters={"type": "object", "properties": {}, "required": []},
        mcp_manager=backend,
    )

    assert await tool.execute() == "(MCP tool call failed: boom)"


@pytest.mark.asyncio
async def test_mcp_resource_tool_formats_against_pure_backend() -> None:
    client = _ScriptedClient(resource_results=[{"success": False, "error": "denied"}])
    backend = _PureBackend(client)

    tool = MCPResourceTool(
        server_name="s1",
        resource_name="r",
        uri="u://r",
        description="a resource",
        mcp_manager=backend,
    )
    assert await tool.execute() == "(MCP resource read failed: denied)"


@pytest.mark.asyncio
async def test_mcp_prompt_tool_formats_against_pure_backend() -> None:
    client = _ScriptedClient(prompt_results=[{"success": False, "error": "nope"}])
    backend = _PureBackend(client)

    tool = MCPPromptTool(
        server_name="s1",
        prompt_name="intro",
        description="intro",
        arguments_def=[],
        mcp_manager=backend,
    )
    assert await tool.execute() == "(MCP prompt call failed: nope)"


def test_mcp_backend_abc_has_no_reconnect_with_retry() -> None:
    """Guard: the ABC surface must not gain a reconnect method.

    If it did, consumers could (re)grow a dependency on it and the facade-
    compatibility guarantee would silently break.
    """
    assert not hasattr(McpBackend, "reconnect_with_retry")
