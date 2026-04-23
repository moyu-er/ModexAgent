"""Dynamic schema protocol for tools.

This module defines `DynamicSchemaProvider` Protocol for context-aware schema generation.

Import `Tool` from `framework.core.tool_manager` instead.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DynamicSchemaProvider(Protocol):
    """为工具提供动态、上下文相关的 Schema 生成能力。

    实现此协议的工具可根据调用者上下文（如当前 Agent 名称、会话 ID 等）
    动态调整其描述或参数定义，避免多会话并发下的共享状态竞争。
    """

    def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """获取动态 Schema（供 LLM 使用）。

        Args:
            caller_context: 调用者上下文，例如 {"agent_name": "main"}。

        Returns:
            OpenAI 格式的工具定义字典。
        """
        ...


__all__ = ["DynamicSchemaProvider"]
