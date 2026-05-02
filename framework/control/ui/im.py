"""IMUserInterface — IM 即时通讯交互。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time
from collections.abc import Mapping, Sequence
from uuid import uuid4

from framework.control.channel import ControlChannel
from framework.control.types import ControlCommandType, ControlScope
from framework.control.ui.abc import ControlUserInterface
from framework.core.types import OutputMessage
from framework.pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)


class IMUserInterface(ControlUserInterface):
    """IM 交互（QQ/Discord/Telegram 等）。

    依赖 OutputAdapter 发送消息，ControlChannel 等待命令响应。
    """

    def __init__(
        self,
        *,
        output_adapter: OutputAdapter,
        channel: ControlChannel,
    ) -> None:
        self._output = output_adapter
        self._channel = channel

    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        msg_id = uuid4().hex[:12]
        try:
            msg = OutputMessage(
                content=content,
                metadata=dict(metadata or {}),
            )
            await self._output.send(msg, session_id)
        except Exception:
            logger.exception(
                "IMUserInterface.render_message failed: session=%s", session_id
            )
        return msg_id

    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> str | None:
        # Send the question as a message first, then poll for response
        await self.render_message(session_id, question, metadata)
        scope = ControlScope(session_id=session_id)
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            cmds = await self._channel.drain(
                scope,
                limit=1,
                command_types={ControlCommandType.APPROVAL_RESPONSE},
            )
            for cmd in cmds:
                action = str(cmd.payload.get("action", ""))
                if action in options:
                    return action
            await asyncio.sleep(0.3)
        return None

    async def update_message(
        self,
        session_id: str,
        message_id: str,
        content: str,
    ) -> None:
        with contextlib.suppress(Exception):
            msg = OutputMessage(
                content=content,
                metadata={"_edit_id": message_id},
            )
            await self._output.send(msg, session_id)
