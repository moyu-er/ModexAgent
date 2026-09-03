"""MCP resilience at the Stage-4 seam (ticket 10).

Ticket 10 moved per-agent MCP loading from the BIZ strategy helper into
``assemble_native_agent`` (the FW loader reads the chain's shared-connection
handle). The resilience contract is unchanged: an MCP selection that cannot
be resolved or connected must NEVER block the agent's tool manager or pool
creation. Pinned here at the loader seam the strategy path now shares.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.tools.mcp_loader import load_per_agent_mcp
from modex_agent.tools.manager import InMemoryToolManager


def _tool_manager_with_sentinel() -> InMemoryToolManager:
    """A tool manager already holding a registered tool (the agent's other
    products) — the resilience assertion target."""

    class _SentinelTool:
        name = "sentinel"

        async def execute(self, *args: object, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
            return "ok"

    tm = InMemoryToolManager()
    tm.register(_SentinelTool())  # type: ignore[arg-type]
    return tm


@pytest.mark.asyncio
async def test_mcp_empty_selection_skips_loading(tmp_path: Path) -> None:
    """Empty selection returns None without touching the filesystem."""
    tm = _tool_manager_with_sentinel()

    backend = await load_per_agent_mcp(tm, [], tmp_path, "main")

    assert backend is None
    assert tm.list_tools() == ["sentinel"]


@pytest.mark.asyncio
async def test_mcp_failure_does_not_block_tool_manager(tmp_path: Path) -> None:
    """An unresolvable MCP selection (no registry.json on disk) drops to
    ``None`` with a warning — the previously-registered tools survive and
    agent assembly proceeds."""
    tm = _tool_manager_with_sentinel()

    backend = await load_per_agent_mcp(
        tm, ["playwright"], tmp_path / "missing", "main"
    )

    assert backend is None
    assert tm.list_tools() == ["sentinel"]
