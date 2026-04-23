"""Truncation compression strategy."""

from collections.abc import Sequence
from typing import Any

from framework.memory.compression.tool_chain import _find_safe_truncation_count
from framework.memory.core.compression import (
    CompressionContext,
    CompressionResult,
    CompressionStrategy,
)
from framework.memory.core.message import ChatMessage


class TruncationStrategy(CompressionStrategy):
    """简单截断策略：保留最近的 N 条消息。

    超出 target_count 的头部消息会被移除。
    截断时保证 tool-call 链完整，不会留下孤立的 tool result。
    适用于快速控制消息数量，不生成摘要。
    """

    def __init__(self, target_count: int = 10):
        self.target_count = target_count

    async def compress(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: CompressionContext,
    ) -> CompressionResult:
        _ = context  # strategy is parameter-driven, context unused
        if len(messages) <= self.target_count:
            return CompressionResult(summary="", pruned_messages=[])

        excess = len(messages) - self.target_count
        safe_excess = _find_safe_truncation_count(messages, excess)
        pruned = messages[:safe_excess]
        remaining = messages[safe_excess:]
        return CompressionResult(
            summary=f"[Truncation] Removed {safe_excess} oldest messages",
            pruned_messages=pruned,
            remaining_messages=remaining,
            importance=0.3,
        )
