"""Tests for ``McpBackend`` ABC default delegation.

The six query/invocation members (``list_tools``/``list_resources``/``list_prompts``
/``execute_tool``/``read_resource``/``get_prompt``) are pure delegation: they route
through ``_client_for`` and return empty/error values when no client is bound.
``MCPClientManager`` and the future shared-connection facade both inherit them
unchanged, so these behaviours are pinned at the ABC level.
"""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import BaseMCPClient


class _StubClient(BaseMCPClient):
    """Minimal ``BaseMCPClient`` stand-in capturing calls and returning canned data.

    ``BaseMCPClient.__init__`` sets the attributes the ABC methods touch, and the
    concrete ``initialize`` is abstract — we provide a no-op so the stub can be
    instantiated without a real MCP session.
    """

    def __init__(self) -> None:  # noqa: D401 - test stub
        super().__init__(name="stub")
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        self.calls.append(("list_tools", (), {}))
        return [{"name": "t1"}]

    async def list_resources(self) -> list[dict[str, Any]]:  # type: ignore[override]
        self.calls.append(("list_resources", (), {}))
        return [{"name": "r1", "uri": "u://r1"}]

    async def list_prompts(self) -> list[dict[str, Any]]:  # type: ignore[override]
        self.calls.append(("list_prompts", (), {}))
        return [{"name": "p1"}]

    async def call_tool(  # type: ignore[override]
        self,
        tool_name: str,
        params: dict[str, Any],
        timeout: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(("call_tool", (tool_name, params), {"timeout": timeout}))
        return {"success": True, "result": f"called:{tool_name}"}

    async def read_resource(  # type: ignore[override]
        self,
        uri: str,
        timeout: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(("read_resource", (uri,), {"timeout": timeout}))
        return {"success": True, "result": f"read:{uri}"}

    async def get_prompt(  # type: ignore[override]
        self,
        prompt_name: str,
        arguments: dict[str, Any] | None = None,
        timeout: int = 5,
    ) -> dict[str, Any]:
        self.calls.append(
            ("get_prompt", (prompt_name,), {"arguments": arguments, "timeout": timeout})
        )
        return {"success": True, "result": f"prompt:{prompt_name}"}


class _FakeBackend(McpBackend):
    """A ``McpBackend`` whose ``_client_for`` is driven by a dict, for testing."""

    def __init__(self, clients: dict[str, _StubClient] | None = None) -> None:
        self._clients = clients or {}
        self.released = False

    @property
    def connected_servers(self) -> list[str]:
        return list(self._clients.keys())

    def _client_for(self, name: str) -> BaseMCPClient | None:
        return self._clients.get(name)

    async def release(self) -> None:
        self.released = True


@pytest.mark.asyncio
async def test_list_members_delegate_to_client() -> None:
    client = _StubClient()
    backend = _FakeBackend({"s1": client})

    assert await backend.list_tools("s1") == [{"name": "t1"}]
    assert await backend.list_resources("s1") == [{"name": "r1", "uri": "u://r1"}]
    assert await backend.list_prompts("s1") == [{"name": "p1"}]

    assert [c[0] for c in client.calls] == ["list_tools", "list_resources", "list_prompts"]


@pytest.mark.asyncio
async def test_list_members_return_empty_when_no_client() -> None:
    backend = _FakeBackend({})

    assert await backend.list_tools("absent") == []
    assert await backend.list_resources("absent") == []
    assert await backend.list_prompts("absent") == []


@pytest.mark.asyncio
async def test_execute_tool_delegates_and_forwards_timeout() -> None:
    client = _StubClient()
    backend = _FakeBackend({"s1": client})

    result = await backend.execute_tool("s1", "sum", {"a": 1}, timeout=42)

    assert result == {"success": True, "result": "called:sum"}
    name, args, kwargs = client.calls[-1]
    assert name == "call_tool"
    assert args == ("sum", {"a": 1})
    assert kwargs == {"timeout": 42}


@pytest.mark.asyncio
async def test_read_resource_delegates_and_forwards_timeout() -> None:
    client = _StubClient()
    backend = _FakeBackend({"s1": client})

    result = await backend.read_resource("s1", "u://x", timeout=7)

    assert result == {"success": True, "result": "read:u://x"}
    assert client.calls[-1][0] == "read_resource"
    assert client.calls[-1][1] == ("u://x",)
    assert client.calls[-1][2] == {"timeout": 7}


@pytest.mark.asyncio
async def test_get_prompt_delegates_and_forwards_arguments_and_timeout() -> None:
    client = _StubClient()
    backend = _FakeBackend({"s1": client})

    result = await backend.get_prompt("s1", "intro", arguments={"k": "v"}, timeout=9)

    assert result == {"success": True, "result": "prompt:intro"}
    name, args, kwargs = client.calls[-1]
    assert name == "get_prompt"
    assert args == ("intro",)
    assert kwargs == {"arguments": {"k": "v"}, "timeout": 9}


@pytest.mark.asyncio
async def test_invocation_members_return_error_dict_when_no_client() -> None:
    backend = _FakeBackend({})

    assert await backend.execute_tool("absent", "t", {}) == {
        "success": False,
        "error": "MCP server not connected: absent",
    }
    assert await backend.read_resource("absent", "u://x") == {
        "success": False,
        "error": "MCP server not connected: absent",
    }
    assert await backend.get_prompt("absent", "p") == {
        "success": False,
        "error": "MCP server not connected: absent",
    }


def test_mcpbackend_cannot_be_instantiated_directly() -> None:
    """The ABC must not be concrete — abstract members must be implemented."""
    with pytest.raises(TypeError):
        McpBackend()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_mcp_client_manager_is_a_mcp_backend() -> None:
    """``MCPClientManager`` realises the ABC and inherits the default methods."""
    from modex_agent.tools.mcp import MCPClientManager

    manager = MCPClientManager()

    assert isinstance(manager, McpBackend)
    # Default delegation is inherited — absent server yields the empty/error shape.
    assert await manager.list_tools("absent") == []
    assert await manager.execute_tool("absent", "t", {}) == {
        "success": False,
        "error": "MCP server not connected: absent",
    }
