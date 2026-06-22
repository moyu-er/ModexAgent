"""ContextGovernance ABC — pre-LLM context treatment contract.

The abstract interface lives in core so that core.context and
agents.react.nodes.llm can depend on it without importing from memory.
Concrete implementations stay in framework.memory.context_governance.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ContextGovernance(ABC):
    """轮内上下文治理抽象基类。

    在每次 LLM 调用前对消息列表进行调整，确保不超出 token 预算
    或上下文窗口限制。所有实现必须返回新的消息列表副本，不得
    修改原始输入。
    """

    @abstractmethod
    async def apply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """应用治理策略，返回调整后的消息列表副本。"""
        ...
