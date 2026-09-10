"""Origin annotations of the direct tool-registration paths (T3).

Two FW paths register tools OUTSIDE the compiled roster, each with a
dedicated origin semantic:

- ``ensure_input_companion`` registers the structural ``bash_input``
  companion as INTERNAL — the name slot is protected, so a later roster
  entry named ``bash_input`` cannot break the persistent-shell pairing;
- ``load_per_agent_mcp`` registers adapted MCP tools as EXTERNAL — they
  rely on namespace-prefix isolation and never displace an existing slot
  on an unexpected same-name collision.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.tool_manager import Tool, ToolOrigin, ToolOverrideRecord
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.mcp.client import BaseMCPClient
from modex_agent.tools.mcp.injector import MCPTransportInjector
from modex_agent.tools.mcp.registry import McpConnectionRegistry
from modex_agent.tools.mcp_loader import load_per_agent_mcp
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
    ensure_input_companion,
    persistent_bash_supported,
)


class _FakeTool(Tool):
    """Minimal named tool for slot-collision assertions."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=f"fake {name}", parameters={})

    async def execute(self, **kwargs: Any) -> str:  # pragma: no cover
        return "ok"


# ── ensure_input_companion → INTERNAL ───────────────────────────────────────


class TestBashInputCompanionOrigin:
    @pytest.mark.skipif(
        not persistent_bash_supported(), reason="persistent bash needs a POSIX pty"
    )
    def test_companion_slot_is_protected_against_roster_registration(self) -> None:
        """The companion registers INTERNAL: a later roster-style
        registration of ``bash_input`` raises instead of displacing the
        structural pairing."""
        manager = InMemoryToolManager()
        bash = PersistentBashTool()
        ensure_input_companion(manager, bash)

        assert isinstance(manager.get_tool("bash_input"), BashInputTool)
        with pytest.raises(ValueError, match="bash_input"):
            manager.register(_FakeTool("bash_input"), origin=ToolOrigin.PRESET)
        assert isinstance(manager.get_tool("bash_input"), BashInputTool)


# ── load_per_agent_mcp → EXTERNAL ───────────────────────────────────────────


class _StubClient(BaseMCPClient):
    """Advertises one canned tool (the registry-test stub pattern)."""

    def __init__(self, name: str, *, tools: list[dict[str, Any]]) -> None:
        super().__init__(name=name)
        self._stub_tools = tools
        self._initialized = True

    async def initialize(self) -> bool:  # type: ignore[override]
        return True

    async def list_tools(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return list(self._stub_tools)


def _connect_fn(
    tools: list[dict[str, Any]],
) -> Callable[..., Awaitable[BaseMCPClient]]:
    async def _connect(
        name: str,
        server_config: dict[str, Any],
        *,
        injector: MCPTransportInjector,
        stack: AsyncExitStack,
    ) -> BaseMCPClient:
        del server_config, injector, stack
        return _StubClient(name, tools=tools)

    return _connect


_ONE_TOOL = [{"name": "search", "description": "stub tool", "inputSchema": {}}]


class TestMcpLoaderOrigin:
    async def test_mcp_tools_register_external_and_never_displace(self, tmp_path: Path) -> None:
        """A same-named EXTERNAL registration onto an occupied slot is
        skipped (the pre-registered winner stays) — MCP collisions never
        silently replace an existing tool."""
        registry = McpConnectionRegistry(
            {"s1": {"transport": "stdio", "command": "echo", "args": ["hi"]}},
            connect_fn=_connect_fn(_ONE_TOOL),
        )
        try:
            manager = InMemoryToolManager()
            first = _FakeTool("s1_search")
            manager.register(first, origin=ToolOrigin.PRESET)

            backend = await load_per_agent_mcp(
                manager, ["s1"], tmp_path, "agent-a", registry=registry
            )

            assert backend is not None
            assert manager.get_tool("s1_search") is first
            assert manager.override_records == ()
        finally:
            await registry.shutdown()

    async def test_mcp_slot_is_classified_external(self, tmp_path: Path) -> None:
        """An empty-slot EXTERNAL registration classifies the slot; a later
        roster registration displaces it WITH an audited record whose
        ``displaced_origin`` is EXTERNAL."""
        registry = McpConnectionRegistry(
            {"s1": {"transport": "stdio", "command": "echo", "args": ["hi"]}},
            connect_fn=_connect_fn(_ONE_TOOL),
        )
        try:
            manager = InMemoryToolManager()
            await load_per_agent_mcp(manager, ["s1"], tmp_path, "agent-a", registry=registry)

            assert "s1_search" in manager.list_tools()
            winner = _FakeTool("s1_search")
            manager.register(winner, origin=ToolOrigin.CAPABILITY_DERIVED)
            assert manager.get_tool("s1_search") is winner
            assert manager.override_records == (
                ToolOverrideRecord(
                    tool_name="s1_search",
                    winner_origin=ToolOrigin.CAPABILITY_DERIVED,
                    displaced_origin=ToolOrigin.EXTERNAL,
                ),
            )
        finally:
            await registry.shutdown()
