"""Tool-manager composition for graph-scoped tools."""

from __future__ import annotations

from modex_agent.core.tool_manager import Tool, ToolManager
from modex_agent.tools.manager import InMemoryToolManager


class GraphToolPreset:
    """Build independent tool managers extended with graph tools."""

    def __init__(
        self,
        graph_tools: list[Tool],
        excluded_base_tools: set[str] | None = None,
    ) -> None:
        self._graph_tools = list(graph_tools)
        self._excluded_base_tools = excluded_base_tools or set()

    def build_tool_manager(self, base: ToolManager) -> InMemoryToolManager:
        """Copy base tools (skipping excluded), then register graph-scoped overrides."""
        tool_manager = InMemoryToolManager()
        for tool_name in base.list_tools():
            if tool_name in self._excluded_base_tools:
                continue
            tool = base.get_tool(tool_name)
            if tool is None:
                message = f"ToolManager listed an unavailable tool: {tool_name}"
                raise RuntimeError(message)
            tool_manager.register(tool)
        for tool in self._graph_tools:
            tool_manager.register(tool)
        return tool_manager


__all__ = ["GraphToolPreset"]
