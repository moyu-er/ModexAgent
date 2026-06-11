"""Tool注册表

提供统一的工具注册和管理接口。

注意：ToolRegistry 现在是 ToolManager 的兼容包装层，
推荐使用 InMemoryToolManager 直接替代。
"""

from typing import Any

from ..core.tool_manager import InMemoryToolManager


class ToolRegistry(InMemoryToolManager):
    """
    工具注册表（兼容层）。

    基于 InMemoryToolManager 实现，保持向后兼容的 API。
    新代码建议直接使用 InMemoryToolManager。

    Example:
        registry = ToolRegistry()
        registry.register(ReadFileTool())
        registry.register(WriteFileTool())

        # 获取工具定义（用于LLM）
        schemas = registry.get_definitions()
    """

    def __init__(self):
        # 使用默认配置初始化 ToolManager
        super().__init__()

    def get_definitions(self) -> list[dict[str, Any]]:
        """
        获取所有工具的schema定义（用于LLM）。

        Returns:
            OpenAI function calling格式的工具定义列表
        """
        return self.get_tool_descriptions()
