"""QQ Bot Sink + KindGate.

Split from ``bot/adapters/qq.py``. Logic unchanged; only the module boundary
moved (and the emitter face migrated to the turn-event sink).
"""

from __future__ import annotations

import logging

from modex_agent.adapters.emitter import BufferingSink
from modex_agent.core.emitter import KindGate
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)


class QQEmitterConfig:
    """QQ Bot 的 KindGate 配置工厂。

    Kind 字面量是核心 ``TurnEvent`` 的 kind（旧枚举事件名迁移：
    model_output→text, tool_call_start→tool_call, tool_call_end→tool_result,
    final_output→turn_finished, error→turn_errored）。``iteration_finished``
    不在旧集合中，但它是缓冲投递策略（SEGMENT）的 flush 边界，必须启用。
    """

    @staticmethod
    def minimal() -> KindGate:
        """最小配置 - 接收模型内容、工具调用日志和最终结果"""
        return QQEmitterConfig.custom()

    @staticmethod
    def with_tools() -> KindGate:
        """带工具调用配置"""
        return QQEmitterConfig.custom()

    @staticmethod
    def debug() -> KindGate:
        """调试配置 - 接收所有事件"""
        return KindGate()  # 默认启用所有

    @staticmethod
    def custom(enabled: set | None = None, disabled: set | None = None) -> KindGate:
        """自定义配置"""
        if enabled is None:
            enabled = {
                "text",
                "tool_call",
                "tool_result",
                "turn_finished",
                "turn_errored",
                "iteration_finished",
            }
        return KindGate(
            enabled_kinds=frozenset(enabled),
            disabled_kinds=frozenset(disabled or set()),
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
