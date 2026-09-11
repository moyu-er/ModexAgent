"""Tool-manager composition for graph-scoped tools."""

from __future__ import annotations

from modex_agent.core.tool_group import ToolGroup
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
        grouped_names: set[str] = set()
        for group in base.tool_groups:
            member_names = {tool.name for tool in group.tools}
            grouped_names.update(member_names)
            excluded = member_names & self._excluded_base_tools
            if excluded and group.anchor not in self._excluded_base_tools:
                raise ValueError(
                    f"Tool group '{group.anchor}' cannot be partially excluded: "
                    f"{sorted(excluded)}"
                )
            if group.anchor in self._excluded_base_tools:
                continue
            tool_manager.register_group(
                ToolGroup(
                    anchor=group.anchor,
                    variant=group.variant,
                    tools=group.tools,
                    resource=None,
                ),
                origin=base.origin_of(group.anchor),
            )
        for tool_name in base.list_tools():
            if tool_name in grouped_names:
                continue
            if tool_name in self._excluded_base_tools:
                continue
            tool = base.get_tool(tool_name)
            if tool is None:
                message = f"ToolManager listed an unavailable tool: {tool_name}"
                raise RuntimeError(message)
            tool_manager.register(tool, origin=base.origin_of(tool_name))
        for tool in self._graph_tools:
            tool_manager.register(tool)
        return tool_manager


__all__ = ["GraphToolPreset"]
