"""清理策略核心 -- 与存储层、框架生命周期完全解耦.

ToolCallCleanupPolicy 是一个纯策略类:
- 只操作消息列表 (list[dict]), 不依赖任何框架类型
- 可独立单元测试, 无需 Mock AgentContext / MemoryContext
- 策略规则显式编码, 易于理解和修改
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ToolCallCleanupPolicy:
    """Tool-Call 链清理策略.

    职责单一: 给定消息列表, 判断是否满足"ReAct 已完成"条件,
    若满足则返回清理后的列表 (含中断轮次模拟替换), 否则原样返回.
    """

    # 固定前缀, 用于识别模拟 assistant 消息
    _SIMULATED_PREFIX: str = "[SIMULATED: ReAct iteration limit reached] "
    _SIMULATED_MSG: str = _SIMULATED_PREFIX + (
        "This turn was interrupted because the ReAct tool-call loop "
        "hit the maximum iteration limit. No complete assistant response "
        "was generated. This placeholder preserves conversation continuity."
    )

    @classmethod
    def is_simulated(cls, message: dict[str, Any]) -> bool:
        """通过固定前缀识别模拟 assistant 消息."""
        content = message.get("content", "")
        return isinstance(content, str) and content.startswith(cls._SIMULATED_PREFIX)

    def should_cleanup(self, messages: list[dict[str, Any]]) -> bool:
        """判断消息列表是否满足清理条件."""
        if not messages:
            return False
        last = messages[-1]
        return last.get("role") == "assistant" and not last.get("tool_calls")

    def clean(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """执行清理, 返回新列表 (不修改输入).

        全量扫描并移除:
        - role=tool 的消息
        - role=assistant 且携带 tool_calls 的消息
        其余消息（user、agent、无 tool_calls 的 assistant 等）原样保留。
        """
        if not self.should_cleanup(messages):
            return list(messages)

        result = [
            m for m in messages
            if m.get("role") != "tool"
            and not (m.get("role") == "assistant" and m.get("tool_calls") and len(m.get("tool_calls")) >= 1)
        ]

        logger.debug(
            "ToolCallCleanupPolicy: %d -> %d messages",
            len(messages),
            len(result),
        )
        return result
