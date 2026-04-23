from __future__ import annotations

from collections.abc import Callable

from framework.core.tool_manager import InMemoryToolManager, Tool, ToolManager


class ToolAssemblyKit:
    """原子级工具克隆工具箱，用于将源 ToolManager 中的工具子集组装到新的 InMemoryToolManager 中。"""

    @staticmethod
    def assemble(source: ToolManager, names: list[str]) -> InMemoryToolManager:
        """按名称克隆指定工具到新的 InMemoryToolManager。

        不存在的名称会被静默忽略。有状态工具如果实现了 ``clone()``，
        会复制克隆后的实例而不是共享引用。
        """
        target = InMemoryToolManager()
        for name in names:
            tool = source.get_tool(name)
            if tool is not None:
                copied = tool.clone() if getattr(type(tool), "clone", None) is not Tool.clone else tool
                target.register(copied)
        return target

    @staticmethod
    def filter(source: ToolManager, predicate: Callable[[Tool], bool]) -> InMemoryToolManager:
        """按谓词筛选工具并克隆到新的 InMemoryToolManager。

        有状态工具如果实现了 ``clone()``，会复制克隆后的实例。
        """
        target = InMemoryToolManager()
        for name in source.list_tools():
            tool = source.get_tool(name)
            if tool is not None and predicate(tool):
                copied = tool.clone() if getattr(type(tool), "clone", None) is not Tool.clone else tool
                target.register(copied)
        return target
