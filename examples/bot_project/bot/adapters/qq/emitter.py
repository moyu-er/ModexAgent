"""QQ Bot Sink.

Split from ``bot/adapters/qq.py``. Logic unchanged; only the module boundary
moved (and the emitter face migrated to the turn-event sink).
"""

from __future__ import annotations

import logging

from modex_agent.adapters.emitter import BufferingSink
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)


class QQBotEmitter(BufferingSink):
    """QQ Bot 事件处理器

    业务逻辑：
    - 模型内容：按投递策略缓冲/发送给用户
    - 思维链：只记日志，不发用户
    - 工具调用：记录到日志，不发给用户
    """

    async def _dispatch(self, event: TurnEvent) -> None:
        """处理业务事件：先做 kind 级日志，再交由基类完成缓冲、flush、
        附件和错误发送等通用逻辑。"""
        match event:
            case TurnReasoningEvent(text=text):
                logging.getLogger("bot.reasoning").info(f"[Reasoning] {text}")
            case TurnToolCallEvent(
                tool_name=tool_name, call_id=call_id, arguments=arguments
            ):
                logging.getLogger("bot.tools").info(
                    f"[Tool Call] {tool_name} args={dict(arguments)} call_id={call_id}"
                )
            case TurnToolResultEvent(
                tool_name=tool_name, error=error, seq=seq
            ):
                logging.getLogger("bot.tools").info(
                    f"[Tool Result] {tool_name} error={error} seq={seq}"
                )
            case _:
                pass

        # 基类负责 text 缓冲、iteration_finished/turn_finished flush、
        # attachments 转发和 turn_errored 错误发送等通用逻辑
        await super()._dispatch(event)
