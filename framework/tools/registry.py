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

        # 执行工具
        result = await registry.execute("read_file", {"path": "/tmp/test.txt"})
    """

    def __init__(self):
        # 使用默认配置初始化 ToolManager
        super().__init__()

    def get(self, tool_name: str) -> Any | None:
        """
        获取工具。

        Args:
            tool_name: 工具名称

        Returns:
            工具实例，不存在则返回None
        """
        return self.get_tool(tool_name)

    def has(self, tool_name: str) -> bool:
        """
        检查工具是否存在。

        Args:
            tool_name: 工具名称

        Returns:
            是否存在
        """
        return self.is_registered(tool_name)

    def get_definitions(self) -> list[dict[str, Any]]:
        """
        获取所有工具的schema定义（用于LLM）。

        Returns:
            OpenAI function calling格式的工具定义列表
        """
        return self.get_tool_descriptions()

    async def execute_tool(self, tool_name: str, params: dict[str, Any]) -> str:
        """
        执行工具（兼容层方法，返回字符串而非 ToolResult）。

        Args:
            tool_name: 工具名称
            params: 工具参数

        Returns:
            工具执行结果字符串

        Raises:
            ValueError: 工具不存在或执行失败
        """
        result = await super().execute(tool_name, params)
        if result.error:
            raise ValueError(f"Tool execution failed: {result.error}")
        return str(result.result) if result.result is not None else ""

    def create_copy(self) -> "ToolRegistry":
        """
        创建注册表的副本。

        Returns:
            新的ToolRegistry实例，包含相同的工具
        """
        new_registry = ToolRegistry()
        for tool_name in self.list_tools():
            tool = self.get_tool(tool_name)
            if tool:
                new_registry.register(tool)
        return new_registry

    def filter_tools(self, include: list[str] | None = None) -> "ToolRegistry":
        """
        筛选工具，创建新的注册表。

        Args:
            include: 要包含的工具名称列表，None则包含所有

        Returns:
            新的ToolRegistry实例
        """
        new_registry = ToolRegistry()
        for tool_name in self.list_tools():
            if include is None or tool_name in include:
                tool = self.get_tool(tool_name)
                if tool:
                    new_registry.register(tool)
        return new_registry

    def exclude_tools(self, exclude: list[str]) -> "ToolRegistry":
        """
        排除指定工具，创建新的注册表。

        Args:
            exclude: 要排除的工具名称列表

        Returns:
            新的ToolRegistry实例
        """
        new_registry = ToolRegistry()
        for tool_name in self.list_tools():
            if tool_name not in exclude:
                tool = self.get_tool(tool_name)
                if tool:
                    new_registry.register(tool)
        return new_registry


# 全局注册表实例
global_registry = ToolRegistry()


def tool(name: str = None, description: str = None):
    """工具装饰器，用于将函数注册为工具。

    Example:
        @tool(name="get_weather", description="获取天气")
        def get_weather(location: str) -> str:
            return f"Weather in {location}: Sunny"
    """
    def decorator(func):
        from ..core.tool_manager import FunctionalTool
        tool_name = name or func.__name__
        tool_description = description or func.__doc__ or ""

        # 使用 FunctionalTool 创建工具实例
        # 注意：FunctionalTool 需要 name, description, parameters, func 参数
        tool_instance = FunctionalTool(
            name=tool_name,
            description=tool_description,
            parameters={"type": "object", "properties": {}},  # 简化参数定义
            func=func,
        )
        global_registry.register(tool_instance)
        return func
    return decorator
