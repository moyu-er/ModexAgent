"""Token-window compression strategy."""

from collections.abc import Sequence
from typing import Any

from framework.memory.compression.tool_chain import _fit_token_window
from framework.memory.core.compression import (
    CompressionContext,
    CompressionResult,
    CompressionStrategy,
)
from framework.memory.core.message import ChatMessage


class TokenWindowStrategy(CompressionStrategy):
    """Token 窗口策略：从头部移除消息，直到总 token 数低于目标值。

    保留最新的消息，优先保证最近上下文的完整性。
    移除时保证 tool-call 链完整，不会留下孤立的 tool result。
    """

    def __init__(self, max_tokens: int = 4000):
        self.max_tokens = max_tokens

    async def compress(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: CompressionContext,
    ) -> CompressionResult:
        target = context.target_token_count or self.max_tokens
        remaining, pruned = _fit_token_window(messages, target)
        if not pruned:
            return CompressionResult(summary="", pruned_messages=[], remaining_messages=list(messages))

        return CompressionResult(
            summary=f"[TokenWindow] Removed {len(pruned)} messages to fit ~{target} tokens",
            pruned_messages=pruned,
            remaining_messages=remaining,
            importance=0.4,
        )
