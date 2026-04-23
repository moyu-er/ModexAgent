"""Heuristic importance scorer for short-term memory compression."""

from typing import Any

from framework.memory.core.compression import ImportanceScorer
from framework.memory.core.message import ChatMessage


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
        content = msg.get("content") or ""
        content_lower = content.strip().lower()

        if role == "system":
            return 1.0

        if role == "assistant" and msg.get("tool_calls"):
            return 0.9

        # 对任意角色，如果内容极短且匹配常见填充词，降低重要性
        if len(content_lower) <= 20 and content_lower in self._LOW_VALUE_FILLERS:
            return 0.2

        if role == "tool":
            return 0.5

        if role == "user":
            base = 0.6
            if "?" in content or "？" in content:
                base += 0.1
            if len(content) > 50:
                base += 0.1
            if len(content) > 200:
                base += 0.05
            return min(base, 0.85)

        # 默认中等重要性
        return 0.5
