"""Concrete in-memory ToolManager (moved from core/tool_manager.py, C2).

Core keeps the ``ToolManager`` ABC + shared execute behavior; this module
owns the concrete registry implementation.
"""

from __future__ import annotations

import logging

from modex_agent.core.tool_group import ToolGroup
from modex_agent.core.tool_manager import (
    Tool,
    ToolConfig,
    ToolManager,
    ToolOrigin,
    ToolOverrideRecord,
)

logger = logging.getLogger(__name__)


def _origin_label(origin: ToolOrigin | None) -> str:
    """Render an origin for logs; None = unclassified (legacy) slot."""
    return origin.value if origin is not None else "unclassified"


class InMemoryToolManager(ToolManager):
    """内存中的工具管理器实现"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._origins: dict[str, ToolOrigin] = {}
        self._override_records: list[ToolOverrideRecord] = []
        self._groups: dict[str, ToolGroup] = {}
        self._group_by_tool: dict[str, str] = {}

    def register(
        self,
        tool: Tool,
        config: ToolConfig | None = None,
        *,
        origin: ToolOrigin | None = None,
    ) -> None:
        """注册工具,同名槽位覆盖按 origin 仲裁(见 ABC docstring)。

        决策顺序(分支互斥有序):
        0. name 未注册 → 注册;origin 非 None 则记录归类。
        1. 槽位已是 INTERNAL → raise(受保护名槽,任何来源不得覆盖)。
        2. origin is None → legacy 后写覆盖;清除该名槽归类;不产生记录。
        3. origin == EXTERNAL 且名字已存在 → warning + 跳过。
        4/5/6/7. 见下方内联注释。
        """
        name = tool.name
        group_anchor = self._group_by_tool.get(name)
        if group_anchor is not None:
            if origin is ToolOrigin.EXTERNAL:
                logger.warning(
                    "Tool %r belongs to group %r; skipping EXTERNAL registration",
                    name,
                    group_anchor,
                )
                return
            raise ValueError(
                f"Tool '{name}' belongs to group '{group_anchor}' and cannot be "
                "overwritten by single-tool registration"
            )
        existing_origin: ToolOrigin | None = self._origins.get(name)

        if name not in self._tools:
            # 分支 0: 空槽位 — 直接注册
            self._install(tool, config)
            if origin is not None:
                self._origins[name] = origin
            logger.info(f"Tool registered: {name} (origin={_origin_label(origin)})")
            return

        if existing_origin is ToolOrigin.INTERNAL:
            # 分支 1: INTERNAL 名槽受保护
            raise ValueError(
                f"Tool '{name}' slot is INTERNAL and protected "
                f"(incoming origin={_origin_label(origin)}); "
                "INTERNAL name slots cannot be overwritten"
            )

        if origin is None:
            # 分支 2: legacy 直注 — 后写覆盖,清除归类(回到未分类),不产生记录
            self._install(tool, config)
            self._origins.pop(name, None)
            logger.info(
                f"Tool registered: {name} "
                f"(legacy overwrite, displaced origin={_origin_label(existing_origin)})"
            )
            return

        if origin is ToolOrigin.EXTERNAL:
            # 分支 3: EXTERNAL 依赖命名空间前缀隔离,意外撞名时不覆盖既有名
            logger.warning(
                f"Tool '{name}' already registered "
                f"(origin={_origin_label(existing_origin)}); "
                "skipping EXTERNAL registration of the same name"
            )
            return

        if origin is ToolOrigin.INTERNAL:
            # 分支 6: INTERNAL incoming 视作最高权威,先于优先级表比较处理
            displaced = existing_origin
        elif existing_origin is None:
            # 分支 4: 未分类(legacy 直注)槽位 → 覆盖
            displaced = None
        elif existing_origin is ToolOrigin.EXTERNAL:
            # 分支 5: EXTERNAL 槽位让位 → 覆盖
            displaced = ToolOrigin.EXTERNAL
        else:
            # 分支 7: 双方都是优先级表成员,rank 仲裁(顺序无关)
            existing_rank = ToolOrigin.OVERRIDE_PRIORITY[existing_origin]
            incoming_rank = ToolOrigin.OVERRIDE_PRIORITY[origin]
            if incoming_rank == existing_rank:
                raise ValueError(
                    f"Tool '{name}' registered twice at equal priority "
                    f"(existing origin={existing_origin.value}, "
                    f"incoming origin={origin.value}); "
                    "same-rank same-name collision is a configuration error"
                )
            if incoming_rank < existing_rank:
                logger.debug(
                    f"Tool '{name}' registration skipped: incoming "
                    f"origin={origin.value} (rank {incoming_rank}) loses to "
                    f"existing origin={existing_origin.value} (rank {existing_rank})"
                )
                return
            displaced = existing_origin

        self._install(tool, config)
        self._origins[name] = origin
        self._override_records.append(
            ToolOverrideRecord(
                tool_name=name,
                winner_origin=origin,
                displaced_origin=displaced,
            )
        )
        logger.info(
            f"Tool '{name}' overwritten: winner origin={origin.value}, "
            f"displaced origin={_origin_label(displaced)}"
        )

    def register_group(
        self,
        group: ToolGroup,
        *,
        origin: ToolOrigin | None = None,
    ) -> None:
        """Register a complete group or leave the manager unchanged."""
        names = tuple(tool.name for tool in group.tools)
        if not names or group.anchor not in names:
            raise ValueError(
                f"Tool group '{group.anchor}' must contain its anchor and at least one tool"
            )
        if len(names) != len(set(names)):
            raise ValueError(f"Tool group '{group.anchor}' contains duplicate member names")
        overlap = [name for name in names if name in self._group_by_tool]
        if overlap:
            raise ValueError(
                f"Tool group '{group.anchor}' overlaps registered group members: {overlap}"
            )

        tools_before = dict(self._tools)
        origins_before = dict(self._origins)
        records_before = list(self._override_records)
        try:
            for tool in group.tools:
                self.register(tool, origin=origin)
                if self._tools.get(tool.name) is not tool:
                    raise ValueError(
                        f"Tool group '{group.anchor}' member '{tool.name}' lost "
                        "registration arbitration"
                    )
        except BaseException:
            self._tools = tools_before
            self._origins = origins_before
            self._override_records = records_before
            raise

        registered = ToolGroup(
            anchor=group.anchor,
            variant=group.variant,
            tools=group.tools,
            resource=None,
        )
        self._groups[group.anchor] = registered
        self._group_by_tool.update(dict.fromkeys(names, group.anchor))

    def _install(self, tool: Tool, config: ToolConfig | None) -> None:
        """写入槽位并应用 config(注册即设 config,现行为不变)。"""
        self._tools[tool.name] = tool
        if config:
            tool.config = config

    @property
    def override_records(self) -> tuple[ToolOverrideRecord, ...]:
        """同名槽位覆盖的审计记录(只读快照,按发生顺序)。"""
        return tuple(self._override_records)

    def unregister(self, tool_name: str) -> bool:
        """注销工具"""
        group_anchor = self._group_by_tool.get(tool_name)
        if group_anchor is not None:
            group = self._groups.pop(group_anchor)
            for member in group.tools:
                self._tools.pop(member.name, None)
                self._origins.pop(member.name, None)
                self._group_by_tool.pop(member.name, None)
            logger.debug("Tool group unregistered: %s", group_anchor)
            return True
        if tool_name in self._tools:
            self._tools.pop(tool_name)
            self._origins.pop(tool_name, None)
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
    def tool_groups(self) -> tuple[ToolGroup, ...]:
        return tuple(self._groups.values())

    def get_tool_group(self, tool_name: str) -> ToolGroup | None:
        anchor = self._group_by_tool.get(tool_name)
        return self._groups.get(anchor) if anchor is not None else None

    def origin_of(self, tool_name: str) -> ToolOrigin | None:
        return self._origins.get(tool_name)

    @property
    def tools(self) -> dict[str, Tool]:
        """所有已注册的工具（按名称索引）。调试用，修改 dict 不影响管理器。"""
        return dict(self._tools)

    def __contains__(self, tool_name: str) -> bool:
        """支持 'tool_name in tool_manager' 语法"""
        return self.is_registered(tool_name)


__all__ = ["InMemoryToolManager"]
