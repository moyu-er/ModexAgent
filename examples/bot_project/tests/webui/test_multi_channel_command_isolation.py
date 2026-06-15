"""Multi-channel isolation tests for slash command replies.

Verifies that a special command sent from one IM channel only produces a
reply on that same channel, and does not leak to other IM channels or to
the WebUI channel.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from bot.adapters.channels import (
    ChannelRouterOutputAdapter,
    get_conv_channel,
    set_conv_channel,
)
from bot.adapters.fan_in import FanInInputAdapter
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.pool_router import PoolRouter, PoolSessionStore
from framework.commands.handlers import build_default_builtin_handlers
from framework.commands.processor import SlashCommandProcessor
from framework.control.channel import InMemoryControlChannel
from framework.core.session_id import SessionInfo
from framework.core.types import InputMessage, OutputMessage
from framework.pipeline.adapters import InputAdapter, OutputAdapter
from framework.workspace.handlers import (
    CdCommandHandler,
    ExitCommandHandler,
    PwdCommandHandler,
)


class _RecordingOutputAdapter(OutputAdapter):
    """Records every send/send_delta for later assertions."""

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


class _DummyInputAdapter(InputAdapter):
    """Minimal input adapter that lets tests inject messages one at a time."""

    def __init__(self, channel_name: str) -> None:
        super().__init__()
        self._channel_name = channel_name
        self._queue: asyncio.Queue[InputMessage] = asyncio.Queue()

    @property
    def name(self) -> str:
        return self._channel_name

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        while True:
            msg = await self._queue.get()
            yield msg

    async def inject(self, content: str, session_id: str) -> bool:
        """Inject a message.

        Returns ``True`` if the message was intercepted as a control command
        and should NOT be expected in the normal queue.
        """
        set_conv_channel(session_id, self._channel_name)
        if await self._try_intercept_control(content, session_id):
            return True
        self._queue.put_nowait(
            InputMessage(
                content=content,
                session=SessionInfo.from_str(session_id, default_agent_name="main"),
                source=self._channel_name,
                channel=self._channel_name,
                metadata={"conversation_id": session_id},
            )
        )
        return False


@pytest.fixture(autouse=True)
def _clear_channel_registry():
    """Reset the global conversation->channel map between tests."""
    from bot.adapters import channels

    channels._conversation_channels.clear()
    yield
    channels._conversation_channels.clear()


def _workspace_context(cwd: Path, home: Path):
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
    handlers = list(build_default_builtin_handlers())
    handlers.extend([
        CdCommandHandler(workspace_ctx),
        ExitCommandHandler(workspace_ctx),
        PwdCommandHandler(workspace_ctx),
    ])
    return SlashCommandProcessor(handlers=handlers)


@pytest.mark.asyncio
async def test_control_commands_isolated_across_im_channels():
    """/cd from QQ must only reply on QQ; /exit from Telegram only on Telegram."""
    qq_out = _RecordingOutputAdapter("qq")
    tg_out = _RecordingOutputAdapter("telegram")
    ws_out = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({
        "qq": qq_out,
        "telegram": tg_out,
        "websocket": ws_out,
    })

    workspace_ctx = _workspace_context(Path("/fake/cwd"), Path("/fake/home"))
    processor = _command_processor(workspace_ctx)
    channel = InMemoryControlChannel()

    qq_in = _DummyInputAdapter("qq")
    tg_in = _DummyInputAdapter("telegram")
    fan_in = FanInInputAdapter()
    fan_in.add_source(qq_in)
    fan_in.add_source(tg_in)
    fan_in.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=router,
    )

    intercepted_qq = await qq_in.inject("/cd /tmp", "qq-user-1")
    intercepted_tg = await tg_in.inject("/exit", "tg-user-1")

    assert intercepted_qq is True
    assert intercepted_tg is True

    assert qq_out.messages == [("qq-user-1", "cd: changed to /tmp")]
    assert tg_out.messages == [("tg-user-1", "exit: returned home")]
    assert ws_out.messages == []

    # No message should have leaked into the merged queue.
    assert fan_in._merged_queue.empty()


@pytest.mark.asyncio
async def test_pool_switch_isolated_across_im_channels():
    """/coding from QQ must only notify QQ; /main from Telegram only Telegram."""
    qq_out = _RecordingOutputAdapter("qq")
    tg_out = _RecordingOutputAdapter("telegram")
    ws_out = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({
        "qq": qq_out,
        "telegram": tg_out,
        "websocket": ws_out,
    })

    with tempfile.TemporaryDirectory() as tmp:
        store = PoolSessionStore(Path(tmp))
        pools = {
            "coding": type("Pool", (), {"main_agent_name": "coding"})(),
            "main": type("Pool", (), {"main_agent_name": "main"})(),
        }
        pool_router = PoolRouter(
            input_adapter=None,  # type: ignore[arg-type]
            broker=None,  # type: ignore[arg-type]
            pools=pools,
            session_store=store,
            default_pool="main",
        )

        set_conv_channel("qq-user-2", "qq")
        pool_router.set_pool("qq-user-2", "coding")

        set_conv_channel("tg-user-2", "telegram")
        pool_router.set_pool("tg-user-2", "main")

        assert store.get("qq-user-2", "main") == "coding"
        assert store.get("tg-user-2", "main") == "main"


@pytest.mark.asyncio
async def test_webui_control_command_does_not_leak_to_im():
    """A /cd typed in the WebUI input box must only reply on WebSocket."""
    qq_out = _RecordingOutputAdapter("qq")
    tg_out = _RecordingOutputAdapter("telegram")
    ws_out = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({
        "qq": qq_out,
        "telegram": tg_out,
        "websocket": ws_out,
    })

    workspace_ctx = _workspace_context(Path("/fake/cwd"), Path("/fake/home"))
    processor = _command_processor(workspace_ctx)
    channel = InMemoryControlChannel()

    ws_in = WebSocketInputAdapter()
    ws_in.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=router,
    )

    set_conv_channel("ws-conv-1", "websocket")
    intercepted = await ws_in._try_intercept_control("/cd /tmp", "ws-conv-1.main")

    assert intercepted is True
    assert ws_out.messages == [("ws-conv-1.main", "cd: changed to /tmp")]
    assert qq_out.messages == []
    assert tg_out.messages == []


@pytest.mark.asyncio
async def test_plain_message_not_intercepted_routes_normally():
    """Non-command messages from any channel pass through to the merged queue."""
    qq_out = _RecordingOutputAdapter("qq")
    tg_out = _RecordingOutputAdapter("telegram")
    ws_out = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({
        "qq": qq_out,
        "telegram": tg_out,
        "websocket": ws_out,
    })

    workspace_ctx = _workspace_context(Path("/fake/cwd"), Path("/fake/home"))
    processor = _command_processor(workspace_ctx)
    channel = InMemoryControlChannel()

    qq_in = _DummyInputAdapter("qq")
    fan_in = FanInInputAdapter()
    fan_in.add_source(qq_in)
    fan_in.configure_control_filter(
        control_channel=channel,
        command_processor=processor,
        output_adapter=router,
    )

    intercepted = await qq_in.inject("hello", "qq-user-3")
    assert intercepted is False

    await fan_in.start()
    await asyncio.sleep(0)  # let the pump task move the message

    # The message should appear in the merged queue, and no reply should have
    # been sent anywhere.
    assert fan_in._merged_queue.qsize() == 1
    await fan_in.stop()

    assert qq_out.messages == []
    assert tg_out.messages == []
    assert ws_out.messages == []


@pytest.mark.asyncio
async def test_channel_router_unknown_channel_falls_back_to_websocket():
    """If a conversation has no explicit channel mapping, reply to WebSocket."""
    qq_out = _RecordingOutputAdapter("qq")
    ws_out = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({"qq": qq_out, "websocket": ws_out})

    await router.send(OutputMessage(content="fallback"), "unmapped-conv")

    assert ws_out.messages == [("unmapped-conv", "fallback")]
    assert qq_out.messages == []
