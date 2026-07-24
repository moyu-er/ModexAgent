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
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.adapters.channels import (
    ChannelRouterOutputAdapter,
    set_conv_channel,
)
from bot.adapters.fan_in import FanInInputAdapter

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.multi_agent.pool_router import PoolRouter, PoolSessionStore
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter


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
                session=SessionInfo.from_str(session_id),
                source=self._channel_name,
                channel=self._channel_name,
                metadata={"session_id": session_id},
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


class _FakeAdapter:
    """Test double for InputAdapter that allows attribute assignment."""

    def __init__(self, name: str, current_ws: Path, home: Path) -> None:
        self.name = name
        self.current_ws = current_ws
        self.home = home

    def save_current_ws(self) -> None:
        pass


@pytest.mark.asyncio
async def test_control_commands_isolated_across_im_channels(tmp_path: Path):
    """S2 handles /cd and /exit directly; each adapter's current_ws is updated."""
    from unittest.mock import MagicMock

    from bot.input_pipeline.context import BotInputContext
    from bot.input_pipeline.stages.environment_control import EnvironmentControlStage

    from modex_agent.input_pipeline.envelope import UserInputEnvelope
    from modex_agent.workspace.control import WorkspaceController
    from modex_agent.workspace.models import CdResult

    project_dir = tmp_path / "home"
    project_dir.mkdir()
    workspace_dir = project_dir / "workspace"
    workspace_dir.mkdir()

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
    stage = EnvironmentControlStage(workspace_controller=controller)

    # QQ /cd
    qq_adapter = _FakeAdapter("qq", project_dir, project_dir)
    ctx_qq = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=qq_adapter,  # type: ignore[arg-type]
        current_ws_provider=lambda: project_dir,
    )
    env_qq = UserInputEnvelope(external_id="u1", content="/cd workspace", channel="qq")
    result_qq = await stage.process(env_qq, ctx_qq)
    assert not result_qq.should_continue()
    assert qq_adapter.current_ws == workspace_dir

    # Telegram /exit
    tg_adapter = _FakeAdapter("telegram", workspace_dir, project_dir)
    ctx_tg = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=tg_adapter,  # type: ignore[arg-type]
        current_ws_provider=lambda: workspace_dir,
    )
    env_tg = UserInputEnvelope(external_id="u2", content="/exit", channel="telegram")
    result_tg = await stage.process(env_tg, ctx_tg)
    assert not result_tg.should_continue()
    assert tg_adapter.current_ws == project_dir


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
async def test_webui_control_command_does_not_leak_to_im(tmp_path: Path):
    """A /cd typed in the WebUI input box is handled by S2 for the websocket adapter only."""
    from bot.input_pipeline.context import BotInputContext
    from bot.input_pipeline.stages.environment_control import EnvironmentControlStage

    from modex_agent.input_pipeline.envelope import UserInputEnvelope
    from modex_agent.workspace.control import WorkspaceController
    from modex_agent.workspace.models import CdResult

    qq_out = _RecordingOutputAdapter("qq")
    tg_out = _RecordingOutputAdapter("telegram")
    ws_out = _RecordingOutputAdapter("websocket")
    router = ChannelRouterOutputAdapter({
        "qq": qq_out,
        "telegram": tg_out,
        "websocket": ws_out,
    })

    project_dir = tmp_path / "home"
    project_dir.mkdir()
    workspace_dir = project_dir / "workspace"
    workspace_dir.mkdir()

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
    stage = EnvironmentControlStage(workspace_controller=controller)

    ws_adapter = _FakeAdapter("websocket", project_dir, project_dir)
    ctx_ws = BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=ws_adapter,  # type: ignore[arg-type]
        current_ws_provider=lambda: project_dir,
    )
    env_ws = UserInputEnvelope(external_id="u1", content="/cd workspace", channel="websocket")
    result = await stage.process(env_ws, ctx_ws)

    assert not result.should_continue()
    # WebSocket adapter's current_ws was updated
    assert ws_adapter.current_ws == workspace_dir
    # QQ and Telegram adapters were not touched
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

    from modex_agent.commands.handlers import build_default_builtin_handlers
    from modex_agent.commands.processor import SlashCommandProcessor

    processor = SlashCommandProcessor(handlers=list(build_default_builtin_handlers()))
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
