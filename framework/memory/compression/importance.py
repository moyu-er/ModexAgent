"""Heuristic importance scorer for short-term memory compression."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from framework.memory.core.message import ChatMessage


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


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转为 dict。"""
    return msg.to_dict() if isinstance(msg, ChatMessage) else msg


class HeuristicImportanceScorer(ImportanceScorer):
    """基于启发式规则的消息重要性评分器。

    评分规则（0.0 ~ 1.0）:
    - system 消息: 1.0 (最高优先级)
    - assistant 的 tool_calls 消息: 0.9 (工具调用链条关键)
    - user 消息: 基础 0.6，包含问号或长度较长时加分，最高 0.85
    - tool 结果消息: 0.5
    - 极短的无意义消息 (如 "ok", "thanks"): 0.2
    """

    # 常见的低价值短回复关键词
    _LOW_VALUE_FILLERS: set[str] = {
        "ok",
        "okay",
        "thanks",
        "thank you",
        "got it",
        "sure",
        "yes",
        "no",
        "yep",
        "nope",
        "好的",
        "谢谢",
        "明白了",
        "知道了",
    }

    def score(self, message: ChatMessage | dict[str, Any]) -> float:
        msg = _msg_to_dict(message)
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip().lower()

        # System message: highest priority
        if role == "system":
            return 1.0

        # Assistant with tool_calls: critical for tool chain
        if role == "assistant" and msg.get("tool_calls"):
            return 0.9

        # Tool result message
        if role == "tool":
            return 0.5

        # User message: base + modifiers
        if role == "user":
            if content in self._LOW_VALUE_FILLERS:
                return 0.2
            score = 0.6
            if "?" in content or "？" in content:
                score += 0.15
            if len(content) > 50:
                score += 0.05
            if len(content) > 200:
                score += 0.05
            return min(score, 0.85)

        # Assistant plain message (no tool_calls)
        if role == "assistant":
            if content in self._LOW_VALUE_FILLERS:
                return 0.2
            return 0.55

        return 0.3
