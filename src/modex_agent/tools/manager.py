"""Concrete in-memory ToolManager (moved from core/tool_manager.py, C2).

Core keeps the ``ToolManager`` ABC + shared execute behavior; this module
owns the concrete registry implementation.
"""

from __future__ import annotations

import logging

from modex_agent.core.tool_manager import Tool, ToolConfig, ToolManager

logger = logging.getLogger(__name__)


class InMemoryToolManager(ToolManager):
    """内存中的工具管理器实现"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool, config: ToolConfig | None = None) -> None:
        """注册工具"""
        self._tools[tool.name] = tool
        if config:
            tool.config = config
        logger.info(f"Tool registered: {tool.name}")

    def unregister(self, tool_name: str) -> bool:
        """注销工具"""
        if tool_name in self._tools:
            self._tools.pop(tool_name)
            logger.debug(f"Tool unregistered: {tool_name}")
            return True
        return False

    def get_tool(self, tool_name: str) -> Tool | None:
        """获取工具"""
        return self._tools.get(tool_name)

    def list_tools(self) -> list[str]:
        """列出所有工具"""
        return list(self._tools.keys())

    def is_registered(self, tool_name: str) -> bool:
        """检查工具是否已注册"""
        return tool_name in self._tools

    @property
    def tools(self) -> dict[str, Tool]:
        """所有已注册的工具（按名称索引）。调试用，修改 dict 不影响管理器。"""
        return dict(self._tools)

    def __contains__(self, tool_name: str) -> bool:
        """支持 'tool_name in tool_manager' 语法"""
        return self.is_registered(tool_name)


__all__ = ["InMemoryToolManager"]
