"""LLMStreamWatchInterceptor — LLM 流式输出中监听取消命令。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from framework.control.types import ControlCommandType, ControlScope
from framework.interceptor.abc import (
    InterceptorScope,
    LLMStreamChunk,
    LLMStreamContext,
    LLMStreamNext,
)
from framework.runtime.enums import TurnCustomKey

if TYPE_CHECKING:
    from framework.control.channel import ControlChannel
    from framework.core.agent import AgentContext

logger = logging.getLogger(__name__)


class LLMStreamWatchInterceptor:
    """LLM 流式控制监视器。每 N 个 chunk peek 一次 ControlChannel。

    发现 CANCEL_TURN / CANCEL_RUN 时停止迭代并标记。
    """

    scopes = frozenset([InterceptorScope.LLM_STREAM])

    def __init__(self, channel: ControlChannel, poll_every_n_chunks: int = 3):
        self._channel = channel
        self._poll_every = poll_every_n_chunks

    async def around_llm_stream(
        self,
        ctx: AgentContext[Any],
        call: LLMStreamContext,
        next_stream: LLMStreamNext,
    ) -> AsyncIterator[LLMStreamChunk]:
        scope = ControlScope(session_id=call.session_id)
        counted = 0

        async for chunk in next_stream():
            counted += 1
            if counted % self._poll_every == 0:
                cmds = await self._channel.peek(
                    scope,
                    command_types={
                        ControlCommandType.CANCEL_TURN,
                        ControlCommandType.CANCEL_RUN,
                    },
                )
                for cmd in cmds:
                    if cmd.type in (ControlCommandType.CANCEL_TURN,
                                    ControlCommandType.CANCEL_RUN):
                        state = ctx.runtime.state if ctx.runtime else None
                        if state is not None:
                            state.custom[TurnCustomKey.STREAM_CANCELLED] = True
                        yield LLMStreamChunk(
                            finish_reason="cancelled", control_action="cancel",
                        )
                        return
            yield chunk
