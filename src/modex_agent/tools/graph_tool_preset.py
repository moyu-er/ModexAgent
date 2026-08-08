"""Tool-manager composition for graph-scoped tools."""

from __future__ import annotations

from modex_agent.core.tool_manager import InMemoryToolManager, Tool, ToolManager


class GraphToolPreset:
    """Build independent tool managers extended with graph tools."""

    def __init__(self, graph_tools: list[Tool]) -> None:
        self._graph_tools = list(graph_tools)

    def build_tool_manager(self, base: ToolManager) -> InMemoryToolManager:
        """Copy base tools, then register graph-scoped overrides."""
        tool_manager = InMemoryToolManager()
        for tool_name in base.list_tools():
            tool = base.get_tool(tool_name)
            if tool is None:
                message = f"ToolManager listed an unavailable tool: {tool_name}"
                raise RuntimeError(message)
            tool_manager.register(tool)
        for tool in self._graph_tools:
            tool_manager.register(tool)
        return tool_manager


__all__ = ["GraphToolPreset"]
