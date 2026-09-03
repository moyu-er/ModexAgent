"""QQ Bot Emitter + EmitterConfig.

Split from ``bot/adapters/qq.py``. Logic unchanged; only the module boundary
moved.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.agents.react import ReActEvent
from modex_agent.core.events import EmitterConfig


class QQEmitterConfig:
    """QQ Bot 的 Emitter 配置工厂"""

    @staticmethod
    def minimal() -> EmitterConfig:
        """最小配置 - 接收模型内容、工具调用日志和最终结果"""
        return EmitterConfig(
            enabled_events={
                "model_output",
                "tool_call_start",
                "tool_call_end",
                "final_output",
                "error",
            }
        )

    @staticmethod
    def with_tools() -> EmitterConfig:
        """带工具调用配置"""
        return EmitterConfig(
            enabled_events={
                "model_output",
                "tool_call_start",
                "tool_call_end",
                "final_output",
                "error",
            }
        )

    @staticmethod
    def debug() -> EmitterConfig:
        """调试配置 - 接收所有事件"""
        return EmitterConfig()  # 默认启用所有

    @staticmethod
    def custom(enabled: set | None = None, disabled: set | None = None) -> EmitterConfig:
        """自定义配置"""
        return EmitterConfig(
            enabled_events=enabled,
            disabled_events=disabled or set(),
        )


class QQBotEmitter(StreamingAwareEmitter[ReActEvent]):
    """QQ Bot 事件处理器

    业务逻辑：
    - 模型内容：通过 emit_delta 缓冲/发送给用户
    - 思维链：只记日志，不发用户
    - 工具调用：记录到日志，不发给用户
    """

    async def _on_event(self, event: ReActEvent, data: Any = None) -> None:
        """处理业务事件。

        MODEL_OUTPUT 的内容传输由 emit_delta/emit_content 负责，
        此处不重复处理。日志记录后交由基类完成缓冲、flush 和错误发送。
        """
        event_name = event.value if isinstance(event, Enum) else str(event)

        if event_name == "model_reasoning":
            logging.getLogger("bot.reasoning").info(f"[Reasoning] {data}")
        elif event_name == "tool_call_start":
            logging.getLogger("bot.tools").info(f"[Tool Call] {data}")
        elif event_name == "tool_call_end":
            logging.getLogger("bot.tools").info(f"[Tool Result] {data}")

        # 基类负责 reasoning 缓存、final_output flush、error 发送等通用逻辑
        await super()._on_event(event, data)
