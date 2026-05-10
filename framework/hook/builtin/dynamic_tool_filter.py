"""DynamicToolFilterHook — per-iteration 动态增减 tool 列表。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from framework.core.agent import AgentContext
from framework.core.tool_manager import ToolManager

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class DynamicToolFilterHook:
    """per-iteration 动态增减 tool 列表。

    机制：在 before_iteration 中用 FilteredToolManager 包装原始 tool_manager。
    在 before_iteration 开头总是先恢复原始 tool_manager，确保异常路径下也能恢复。

    所有 per-turn 状态存储在 runtime state custom 字段中，避免 pool 模式竞态。
    """

    def __init__(
        self,
        base: ToolManager,
        token_thresholds: dict[int, set[str]] | None = None,
        error_readonly_threshold: int = 3,
    ):
        self._base = base
        self._token_thresholds = dict(
            sorted((k, v) for k, v in (token_thresholds or {}).items())
        ) if token_thresholds else {}
        self._error_readonly_threshold = error_readonly_threshold

    async def before_iteration(self, ctx: AgentContext[Any]) -> None:
        # 总是先恢复 — 以防上个 iteration 的 after_iteration 因异常未执行
        ctx.tool_manager = self._base

        denied: set[str] = set()
        state = ctx.runtime.state if ctx.runtime else None

        # 规则1: Token 预算梯度降级
        usage = (state.custom.get("usage", {}) or {}).get("total_tokens", 0) if state else 0
        for threshold, tools in self._token_thresholds.items():
            if usage > threshold:
                denied.update(tools)

        # 规则2: 连续错误 → 只读
        errors = state.custom.get("consecutive_errors", 0) if state else 0
        if errors >= self._error_readonly_threshold:
            denied.update({"write_file", "shell", "delete_file"})

        if denied:
            from framework.tools.filter import FilteredToolManager

            wrapper = FilteredToolManager(self._base, denied_tools=list(denied))
            ctx.tool_manager = wrapper
            if state is not None:
                state.custom["_dynamic_tool_active"] = True
                state.custom.setdefault("_dynamic_tool_denied", set()).update(denied)

    async def after_iteration(self, ctx: AgentContext[Any]) -> None:
        ctx.tool_manager = self._base
        state = ctx.runtime.state if ctx.runtime else None
        if state is not None:
            state.custom.pop("_dynamic_tool_active", None)
