from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.core.tool_manager import ToolManager, ToolResult

if TYPE_CHECKING:
    from framework.core.tool_manager import Tool


class FilteredToolManager(ToolManager):
    """基于白名单/黑名单过滤的 ToolManager 包装器。"""

    def __init__(
        self,
        base: ToolManager,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ):
        self._base = base
        self._allowed = set(allowed_tools) if allowed_tools is not None else None
        self._denied = set(denied_tools) if denied_tools else None

    def _is_allowed(self, name: str) -> bool:
        if self._denied and name in self._denied:
            return False
        return self._allowed is None or name in self._allowed

    def register(self, tool: Tool, config: Any | None = None) -> None:
        self._base.register(tool, config)

    def unregister(self, tool_name: str) -> bool:
        return self._base.unregister(tool_name)

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._base.get_tool(tool_name) if self._is_allowed(tool_name) else None

    def list_tools(self) -> list[str]:
        return [n for n in self._base.list_tools() if self._is_allowed(n)]

    def is_registered(self, tool_name: str) -> bool:
        return self._base.is_registered(tool_name) and self._is_allowed(tool_name)

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        return [d for d in self._base.get_tool_descriptions() if self._is_allowed(d.get("function", {}).get("name"))]

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        if not self._is_allowed(tool_name):
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' is not allowed by agent policy.",
            )
        return await self._base.execute(tool_name, arguments)
