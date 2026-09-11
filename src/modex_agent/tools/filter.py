from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modex_agent.core.tool_manager import (
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolOrigin,
    ToolOverrideRecord,
    ToolResult,
)

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities
    from modex_agent.core.tool_group import ToolGroup
    from modex_agent.core.tool_manager import Tool


class FilteredToolManager(ToolManager):
    """基于白名单/黑名单过滤的 ToolManager 包装器。"""

    def __init__(
        self,
        base: ToolManager,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ) -> None:
        self._base = base
        self._allowed = set(allowed_tools) if allowed_tools is not None else None
        self._denied = set(denied_tools) if denied_tools else None
        for group in base.tool_groups:
            self._allowed, self._denied = self._policy_with_group(group)

    def _policy_with_group(
        self,
        group: ToolGroup,
    ) -> tuple[set[str] | None, set[str] | None]:
        members = {tool.name for tool in group.tools}
        allowed = set(self._allowed) if self._allowed is not None else None
        denied = set(self._denied) if self._denied is not None else None
        if allowed is not None and allowed & members:
            if group.anchor not in allowed:
                partial = sorted(allowed & members)
                raise ValueError(
                    f"Tool group '{group.anchor}' cannot be partially allowed: {partial}"
                )
            allowed.update(members)
        if denied is not None and denied & members:
            if group.anchor not in denied:
                partial = sorted(denied & members)
                raise ValueError(
                    f"Tool group '{group.anchor}' cannot be partially denied: {partial}"
                )
            denied.update(members)
        return allowed, denied

    def _is_allowed(self, name: str) -> bool:
        if self._denied and name in self._denied:
            return False
        return self._allowed is None or name in self._allowed

    def register(
        self,
        tool: Tool,
        config: ToolConfig | None = None,
        *,
        origin: ToolOrigin | None = None,
    ) -> None:
        self._base.register(tool, config, origin=origin)

    def unregister(self, tool_name: str) -> bool:
        return self._base.unregister(tool_name)

    def register_group(
        self,
        group: ToolGroup,
        *,
        origin: ToolOrigin | None = None,
    ) -> None:
        allowed, denied = self._policy_with_group(group)
        self._base.register_group(group, origin=origin)
        self._allowed = allowed
        self._denied = denied

    def get_tool(self, tool_name: str) -> Tool | None:
        return self._base.get_tool(tool_name) if self._is_allowed(tool_name) else None

    def list_tools(self) -> list[str]:
        return [n for n in self._base.list_tools() if self._is_allowed(n)]

    def is_registered(self, tool_name: str) -> bool:
        return self._base.is_registered(tool_name) and self._is_allowed(tool_name)

    @property
    def tool_groups(self) -> tuple[ToolGroup, ...]:
        return tuple(
            group
            for group in self._base.tool_groups
            if all(self._is_allowed(tool.name) for tool in group.tools)
        )

    def get_tool_group(self, tool_name: str) -> ToolGroup | None:
        if not self._is_allowed(tool_name):
            return None
        return self._base.get_tool_group(tool_name)

    def origin_of(self, tool_name: str) -> ToolOrigin | None:
        return self._base.origin_of(tool_name)

    @property
    def override_records(self) -> tuple[ToolOverrideRecord, ...]:
        """Delegate the override audit to the wrapped registry."""
        return self._base.override_records

    def get_tool_descriptions(
        self, caps: ModelCapabilities | None = None
    ) -> list[dict[str, Any]]:
        return [
            d
            for d in self._base.get_tool_descriptions(caps)
            if self._is_allowed(d.get("function", {}).get("name"))
        ]

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        if not self._is_allowed(tool_name):
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' is not allowed by agent policy.",
            )
        return await self._base.execute(tool_name, arguments, ctx=ctx)
