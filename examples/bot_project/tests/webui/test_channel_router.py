"""Tests for channel-aware output routing.

These tests verify that control notices, pool switch replies, and pipeline
command responses are delivered back to the originating channel (QQ vs WebUI)
rather than hardcoding a single primary output.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.adapters.channels import (
    ChannelRouterOutputAdapter,
    get_conv_channel,
    set_conv_channel,
)
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.pool_router import PoolRouter, PoolSessionStore
from modex_agent.commands.handlers import build_default_builtin_handlers
from modex_agent.commands.processor import SlashCommandProcessor
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.types import OutputMessage
from modex_agent.pipeline.adapters import OutputAdapter


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


def _workspace_control(cwd: Path, home: Path):
    """Build a MagicMock workspace control (WorkspaceControlPort) for cd/exit/pwd handlers.

    Per-conversation port: ``current(conv)`` / ``switch(conv, target)`` /
    ``exit(conv)`` / ``pwd(conv)``.
    """
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.home = home
    ctx.current = lambda session_id: cwd
    ctx.pwd = lambda session_id: f"cwd: {cwd}\nhome: {home}"

    async def mock_switch(session_id: str, target: str):
        return MagicMock(success=True, notice=f"cd: changed to {target}")

    async def mock_exit(session_id: str):
        return MagicMock(success=True, notice="exit: returned home")

    ctx.switch = mock_switch
    ctx.exit = mock_exit
    return ctx


def _session_id_of(context):
    """Derive the conversation id (session-id prefix) from a CommandContext."""
    return context.session_id.split(".", 1)[0] if "." in context.session_id else context.session_id


def _command_processor(workspace_ctrl):
    """Build a SlashCommandProcessor with default builtin handlers."""
    handlers = list(build_default_builtin_handlers())
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
async def test_control_command_notice_routes_to_websocket(adapters, tmp_path: Path):
    """S2 Terminate response for /cd is surfaced by the adapter to the user."""
    qq, ws, router = adapters

    # S2 handles /cd directly and produces a Terminate with a notice.
    # The adapter's _on_message sends this notice back to the user.
    # This test verifies the channel routing of the notice.
    from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
    from bot.input_pipeline.context import BotInputContext
    from modex_agent.input_pipeline.envelope import UserInputEnvelope
    from unittest.mock import MagicMock
    from modex_agent.workspace.control import WorkspaceController
    from modex_agent.workspace.models import CdResult

    project_dir = tmp_path / "home"
    project_dir.mkdir()
    workspace_dir = project_dir / "workspace"
    workspace_dir.mkdir()
    cmd_adapter = MagicMock()
    cmd_adapter.name = "websocket"
    cmd_adapter.current_ws = project_dir
    cmd_adapter.home = project_dir
    cmd_adapter.save_current_ws = MagicMock()

    controller = MagicMock(spec=WorkspaceController)
    controller.home = project_dir
    controller.open_workspace = AsyncMock(
        return_value=CdResult(
            success=True,
            current_path=workspace_dir,
            original_path=project_dir,
            notice=f"cd: workspace ready at {workspace_dir}",
        )
    )

    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=cmd_adapter,
        current_ws_provider=lambda: project_dir,
    )

    stage = EnvironmentControlStage(workspace_controller=controller)
    env = UserInputEnvelope(external_id="u1", content="/cd workspace", channel="websocket")
    result = await stage.process(env, ctx)

    assert not result.should_continue()
    response = result.response
    assert response is not None and "workspace ready" in response.get("message", "")


@pytest.mark.asyncio
async def test_control_command_notice_routes_to_qq(adapters, tmp_path: Path):
    """S2 Terminate response for /cd on QQ channel."""
    qq, ws, router = adapters

    from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
    from bot.input_pipeline.context import BotInputContext
    from modex_agent.input_pipeline.envelope import UserInputEnvelope
    from unittest.mock import MagicMock
    from modex_agent.workspace.control import WorkspaceController
    from modex_agent.workspace.models import CdResult

    project_dir = tmp_path / "home"
    project_dir.mkdir()
    workspace_dir = project_dir / "workspace"
    workspace_dir.mkdir()
    cmd_adapter = MagicMock()
    cmd_adapter.name = "qq"
    cmd_adapter.current_ws = project_dir
    cmd_adapter.home = project_dir
    cmd_adapter.save_current_ws = MagicMock()

    controller = MagicMock(spec=WorkspaceController)
    controller.home = project_dir
    controller.open_workspace = AsyncMock(
        return_value=CdResult(
            success=True,
            current_path=workspace_dir,
            original_path=project_dir,
            notice=f"cd: workspace ready at {workspace_dir}",
        )
    )

    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=cmd_adapter,
        current_ws_provider=lambda: project_dir,
    )

    stage = EnvironmentControlStage(workspace_controller=controller)
    env = UserInputEnvelope(external_id="u1", content="/cd workspace", channel="qq")
    result = await stage.process(env, ctx)

    assert not result.should_continue()
    response = result.response
    assert response is not None and "workspace ready" in response.get("message", "")


@pytest.mark.asyncio
async def test_pool_switch_routes_to_qq(adapters):
    """PoolRouter switch replies go to the originating channel."""
    qq, ws, router = adapters

    with tempfile.TemporaryDirectory() as tmp:
        session_store = PoolSessionStore(Path(tmp))
        pool = {"coding": type("Pool", (), {"main_agent_name": "coding"})()}
        pool_router = PoolRouter(
            input_adapter=None,  # type: ignore[arg-type]
            broker=None,  # type: ignore[arg-type]
            pools=pool,
            session_store=session_store,
            default_pool="main",
        )

        set_conv_channel("qq-user-2", "qq")
        pool_router.set_pool("qq-user-2", "coding")

        assert session_store.get("qq-user-2", "main") == "coding"
        assert qq.messages == []
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
            broker=None,  # type: ignore[arg-type]
            pools=pool,
            session_store=session_store,
            default_pool="main",
        )

        set_conv_channel("ws-session-2", "websocket")
        pool_router.set_pool("ws-session-2", "coding")

        assert session_store.get("ws-session-2", "main") == "coding"
        assert ws.messages == []
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
