"""Compression strategy abstractions for short-term memory."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from framework.memory.core.message import ChatMessage


@dataclass
class CompressionContext:
    """压缩上下文。"""

    token_count: int = 0
    target_token_count: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompressionResult:
    """压缩结果。"""

    summary: str
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5
    pruned_messages: Sequence[ChatMessage | dict[str, Any]] = field(default_factory=list)
    remaining_messages: Sequence[ChatMessage | dict[str, Any]] | None = None


class CompressionStrategy(ABC):
    """短记忆压缩策略抽象基类。"""

    @abstractmethod
    async def compress(
        self,
        messages: Sequence[ChatMessage | dict[str, Any]],
        context: CompressionContext,
    ) -> CompressionResult:
        """压缩消息列表，返回压缩结果。

        Args:
            messages: 待压缩的完整消息列表
            context: 压缩上下文，包含 token 数、目标 token 数等

        Returns:
            CompressionResult: 包含摘要、元数据、重要性评分和**被移除的消息**
                调用方需要从原列表中删除 `pruned_messages` 并保留剩余消息
        """
        pass


class ImportanceScorer(ABC):
    """消息重要性评分抽象基类。

    用于为未来基于重要性的记忆压缩提供可插拔的评分策略。
    """

    @abstractmethod
    def score(self, message: ChatMessage | dict[str, Any]) -> float:
        """计算单条消息的重要性得分。

        Args:
            message: 待评分的消息

        Returns:
            float: 0.0~1.0 的重要性得分，越高表示越重要
        """
        pass

    def score_batch(self, messages: Sequence[ChatMessage | dict[str, Any]]) -> list[float]:
        """批量评分消息列表。

        子类可覆盖此方法以提供性能优化实现。
        """
        return [self.score(m) for m in messages]
