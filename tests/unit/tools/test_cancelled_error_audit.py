from __future__ import annotations

import asyncio

import pytest

from modex_agent.tools.mcp.manager import MCPClientManager
from modex_agent.tools.mcp.registry import McpConnectionRegistry, _ConnectionEntry


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect_in_progress", [False, True])
async def test_reconnect_wait_propagates_caller_cancellation(
    reconnect_in_progress: bool,
) -> None:
    # Given
    registry = McpConnectionRegistry({})
    entry = _ConnectionEntry(name="server", config_hash="hash", config={})
    entry.reconnect_in_progress = reconnect_in_progress
    if reconnect_in_progress:
        entry.reconnect_future = asyncio.get_running_loop().create_future()
    reconnect = asyncio.create_task(registry.request_reconnect(entry))
    await asyncio.sleep(0)

    # When
    reconnect.cancel()

    # Then
    with pytest.raises(asyncio.CancelledError):
        await reconnect


@pytest.mark.asyncio
async def test_mcp_connect_reconnect_propagates_caller_cancellation() -> None:
    """A reconnect cycle cancelled by the caller (e.g. the tool task being
    cancelled) must propagate — ``_connect_single`` used to swallow every
    ``CancelledError`` into "skip server" (ADR-0048 D6 audit finding)."""
    # Given
    manager = MCPClientManager({"server": {"command": "x"}})

    async def hang_connect(
        name: str,
        config: dict[str, object],
        **kwargs: object,
    ) -> None:
        await asyncio.Event().wait()

    from modex_agent.tools.mcp import manager as manager_mod

    original = manager_mod.connect_single_server
    manager_mod.connect_single_server = hang_connect  # type: ignore[assignment]
    try:
        reconnect = asyncio.create_task(manager.reconnect("server"))
        await asyncio.sleep(0)

        # When
        reconnect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reconnect
    finally:
        manager_mod.connect_single_server = original  # type: ignore[assignment]
