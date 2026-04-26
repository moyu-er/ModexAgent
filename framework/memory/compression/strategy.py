"""Message filter strategy abstraction for semantic memory compression."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any


class MessageSemanticValue(Enum):
    """消息语义价值等级。

    - HIGH: 核心语义（user、system、无 tool_calls 的 assistant）
    - MEDIUM: 值得保留的 tool 结果（如 web_search、ask_user）
    - LOW: 可丢弃的 tool 结果（如大段文件内容、shell 输出）
    - DERIVED: assistant 的 tool_calls 请求本身，其价值取决于对应结果是否保留
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    DERIVED = "derived"


class MessageFilterStrategy(ABC):
    """可插拔的消息语义过滤策略。

    用于决定哪些消息值得被压缩/归档，哪些可以被安全丢弃。
    支持对不完整的 tool-call 链进行折叠（collapse），避免产生孤儿消息。
    """

    @abstractmethod
    def classify(self, msg: dict[str, Any]) -> MessageSemanticValue:
        """对单条消息进行语义价值分级。

        Args:
            msg: 单条对话消息（OpenAI 格式）

        Returns:
            该消息的语义价值等级
        """

    @abstractmethod
    def sanitize(
        self, messages: list[dict[str, Any]], *, collapse_orphan_chains: bool = True
    ) -> list[dict[str, Any]]:
        """清洗消息列表。

        移除 LOW 价值消息，保留 HIGH/MEDIUM 消息，
        并对 DERIVED 消息（assistant tool_calls）及其 chain 做完整性检查。
        当一条 tool_calls 消息的所有对应 tool 结果都被丢弃时，
        若 `collapse_orphan_chains=True`，将其折叠为一条普通的 assistant 文本消息；
        否则直接丢弃整条链。

        Args:
            messages: 原始消息列表
            collapse_orphan_chains: 是否将孤儿 tool-call 链折叠为纯文本消息

        Returns:
            清洗后的消息列表
        """
