"""Hybrid compression strategy that composes child strategies in sequence."""

from collections.abc import Sequence
from typing import Any

from framework.memory.core.compression import (
    CompressionContext,
    CompressionResult,
    CompressionStrategy,
)
from framework.memory.core.message import ChatMessage


class HybridCompressionStrategy(CompressionStrategy):
    """组合多个子策略按顺序执行。

    前面的策略负责粗剪（如截断、Token 窗口），
    后面的策略可以负责精剪（如工具链保护、重要性评分）。

    每个子策略在剩余消息上继续执行，pruned_messages 会合并汇总。
    """

    def __init__(self, strategies: list[CompressionStrategy]):
        self.strategies = strategies

    async def compress(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: CompressionContext,
    ) -> CompressionResult:
        working = list(messages)
        all_pruned: list[ChatMessage | dict[str, Any]] = []
        summaries: list[str] = []
        max_importance = 0.0

        for strategy in self.strategies:
            result = await strategy.compress(working, context)
            if result.pruned_messages:
                pruned_ids = {id(m) for m in result.pruned_messages}
                working = [m for m in working if id(m) not in pruned_ids]
                all_pruned.extend(result.pruned_messages)
            if result.summary:
                summaries.append(result.summary)
            if result.importance > max_importance:
                max_importance = result.importance

        return CompressionResult(
            summary=" | ".join(summaries) if summaries else "",
            pruned_messages=all_pruned,
            remaining_messages=working,
            importance=max_importance,
        )
