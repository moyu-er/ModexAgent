"""Tests for channel-aware output routing.

These tests verify that control notices, pool switch replies, and pipeline
command responses are delivered back to the originating channel (QQ vs WebUI)
rather than hardcoding a single primary output.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from bot.adapters.channels import (
    ChannelRouterOutputAdapter,
    get_conv_channel,
    set_conv_channel,
)
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.pool_router import PoolRouter, PoolSessionStore
from framework.commands.handlers import build_default_builtin_handlers
from framework.commands.processor import SlashCommandProcessor
from framework.control.channel import InMemoryControlChannel
from framework.core.types import OutputMessage
from framework.pipeline.adapters import OutputAdapter
from framework.workspace.handlers import (
    CdCommandHandler,
    ExitCommandHandler,
    PwdCommandHandler,
)


class _RecordingOutputAdapter(OutputAdapter):
    """Test double that records every send/send_delta call."""

    def __init__(self, adapter_name: str) -> None:
        self._name = adapter_name
        self.messages: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.messages.append((session_id, message.content or ""))

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        self.messages.append((session_id, delta))

    async def flush_deltas(self, session_id: str) -> None:
        pass


def _workspace_context(cwd: Path, home: Path):
    """Build a MagicMock workspace context for cd/exit/pwd handlers."""
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.current = cwd
    ctx.home = home
    ctx.data_dir = cwd

    async def mock_cd(target: str):
        return MagicMock(success=True, notice=f"cd: changed to {target}")

    async def mock_exit():
        return MagicMock(success=True, notice="exit: returned home")

    ctx.cd = mock_cd
    ctx.exit = mock_exit
    return ctx


def _command_processor(workspace_ctx):
    """Build a SlashCommandProcessor with cd/exit/pwd handlers."""
    handlers = list(build_default_builtin_handlers())
    handlers.extend([
        CdCommandHandler(workspace_ctx),
        ExitCommandHandler(workspace_ctx),
        PwdCommandHandler(workspace_ctx),
    ])
    return SlashCommandProcessor(handlers=handlers)


@pytest.fixture
def adapters():
    """Return a fresh pair of recording adapters and a router."""
    qq = _RecordingOutputAdapter("qq")
    ws = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({"qq": qq, "websocket": ws})
    return qq, ws, router


@pytest.mark.asyncio
async def test_channel_router_routes_by_conversation_channel(adapters):
    """Router delegates to the adapter matching the conversation's channel."""
    qq, ws, router = adapters

    set_conv_channel("user-qq-1", "qq")
    set_conv_channel("conv-ws-1", "websocket")

    await router.send(OutputMessage(content="qq-hi"), "user-qq-1")
    await router.send(OutputMessage(content="ws-hi"), "conv-ws-1.main")

    assert qq.messages == [("user-qq-1", "qq-hi")]
    assert ws.messages == [("conv-ws-1.main", "ws-hi")]


@pytest.mark.asyncio
async def test_channel_router_defaults_to_websocket(adapters):
    """Unknown channels fall back to the websocket adapter."""
    qq, ws, router = adapters

    set_conv_channel("unknown-channel-user", "discord")
    await router.send(OutputMessage(content="fallback"), "unknown-channel-user")

    assert qq.messages == []
    assert ws.messages == [("unknown-channel-user", "fallback")]


@pytest.mark.asyncio
async def test_control_command_notice_routes_to_websocket(adapters):
    """/cd notice from a WebUI-originated session goes to WebSocket output."""
    qq, ws, router = adapters

    workspace_ctx = _workspace_context(Path("/fake/cwd"), Path("/fake/home"))
    processor = _command_processor(workspace_ctx)
    channel = InMemoryControlChannel()

    ws_input = WebSocketInputAdapter()
    ws_input.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=router,
    )

    set_conv_channel("ws-session", "websocket")
    handled = await ws_input._try_intercept_control("/cd /tmp", "ws-session.main")

    assert handled is True
    assert ws.messages == [("ws-session.main", "cd: changed to /tmp")]
    assert qq.messages == []


@pytest.mark.asyncio
async def test_control_command_notice_routes_to_qq(adapters):
    """/cd notice from a QQ-originated session goes to QQ output."""
    qq, ws, router = adapters

    workspace_ctx = _workspace_context(Path("/fake/cwd"), Path("/fake/home"))
    processor = _command_processor(workspace_ctx)
    channel = InMemoryControlChannel()

    ws_input = WebSocketInputAdapter()
    ws_input.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=router,
    )

    # A QQ conversation uses the QQ user_id as the session/conversation id.
    set_conv_channel("qq-user-1", "qq")
    handled = await ws_input._try_intercept_control("/cd /tmp", "qq-user-1")

    assert handled is True
    assert qq.messages == [("qq-user-1", "cd: changed to /tmp")]
    assert ws.messages == []


@pytest.mark.asyncio
async def test_pool_switch_routes_to_qq(adapters):
    """PoolRouter switch replies go to the originating channel."""
    qq, ws, router = adapters

    with tempfile.TemporaryDirectory() as tmp:
        session_store = PoolSessionStore(Path(tmp))
        pool = {"coding": type("Pool", (), {"main_agent_name": "coding"})()}
        pool_router = PoolRouter(
            input_adapter=None,  # type: ignore[arg-type]
            output_adapter=router,
            broker=None,  # type: ignore[arg-type]
            pools=pool,
            session_store=session_store,
            default_pool="main",
        )

        set_conv_channel("qq-user-2", "qq")
        await pool_router._handle_switch("qq-user-2", "coding")

        assert qq.messages == [("qq-user-2", 'switch to "coding" pool')]
        assert ws.messages == []


@pytest.mark.asyncio
async def test_pool_switch_routes_to_websocket(adapters):
    """PoolRouter switch replies go to WebSocket for WebUI conversations."""
    qq, ws, router = adapters

    with tempfile.TemporaryDirectory() as tmp:
        session_store = PoolSessionStore(Path(tmp))
        pool = {"coding": type("Pool", (), {"main_agent_name": "coding"})()}
        pool_router = PoolRouter(
            input_adapter=None,  # type: ignore[arg-type]
            output_adapter=router,
            broker=None,  # type: ignore[arg-type]
            pools=pool,
            session_store=session_store,
            default_pool="main",
        )

        set_conv_channel("ws-session-2", "websocket")
        await pool_router._handle_switch("ws-session-2", "coding")

        assert ws.messages == [("ws-session-2", 'switch to "coding" pool')]
        assert qq.messages == []


@pytest.mark.asyncio
async def test_channel_router_send_delta_delegates(adapters):
    """send_delta/flush_deltas are also routed by conversation channel."""
    qq, ws, router = adapters

    set_conv_channel("qq-user-3", "qq")
    await router.send_delta("delta-qq", "qq-user-3")
    await router.flush_deltas("qq-user-3")

    assert qq.messages == [("qq-user-3", "delta-qq")]
    assert ws.messages == []


@pytest.mark.asyncio
async def test_channel_router_unknown_session_uses_websocket_fallback(adapters):
    """If no channel mapping exists, router falls back to websocket."""
    qq, ws, router = adapters

    # Use a conversation id that has never been registered.
    await router.send(OutputMessage(content="fallback"), "unmapped-conv")

    assert ws.messages == [("unmapped-conv", "fallback")]
    assert qq.messages == []
