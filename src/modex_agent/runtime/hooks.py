"""RuntimeContextHook — 管理 per-turn RuntimeContext 生命周期。

- start_node_turn：解析并缓存 session 的 RuntimeContext，为新 turn 清空
- before_tool_execution：缓存待执行的工具调用列表
- after_tool_execution：将工具调用结果录入 ToolCallRecord

Moved from hook/builtin/runtime_context.py (plan §15 B2).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from modex_agent.core.tool_manager import ToolResult
from modex_agent.hook.abc import (
    AfterToolExecutionHook,
    BeforeToolExecutionHook,
    StartNodeTurnHook,
)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.types import ToolCall


class RuntimeContextHook(StartNodeTurnHook, BeforeToolExecutionHook, AfterToolExecutionHook):
    """通过 hook 接口管理 per-turn RuntimeContext 生命周期。"""

    @property
    def name(self) -> str:
        return "runtime_context_hook"

    _PENDING_KEY = "_pending_tool_calls"

    async def start_node_turn(self, ctx: AgentContext) -> None:
        rt = ctx.runtime
        if rt is None:
            return
        rt_mgr = rt.services.runtime_context_manager
        if rt_mgr is not None and rt.runtime_context is None:
            rt._runtime_context = await rt_mgr.get_context(ctx.session, None)
        rc = rt.runtime_context
        if rc is not None:
            await rc.clear()

    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: Sequence[ToolCall] | None = None,
    ) -> None:
        if ctx.runtime is None:
            return
        rc = ctx.runtime.runtime_context
        if rc is not None and tool_calls:
            await rc.set(self._PENDING_KEY, list(tool_calls))

    async def after_tool_execution(
        self,
        ctx: AgentContext,
        results: Sequence[ToolResult] | None = None,
    ) -> None:
        if ctx.runtime is None:
            return
        rc = ctx.runtime.runtime_context
        if rc is None:
            return
        pending = await rc.get(self._PENDING_KEY, [])
        if not pending:
            return

        result_map: dict[str | None, str] = {}
        if results:
            for r in results:
                if isinstance(r, ToolResult):
                    result_map[r.call_id] = f"Error: {r.error}" if r.error else r.message_content()
                elif isinstance(r, dict) and r.get("role") == "tool":
                    result_map[r.get("tool_call_id")] = r.get("content", "")

        for tool_call in pending:
            call_id = tool_call.call_id
            tool_name = tool_call.tool_name
            arguments = tool_call.arguments or {}
            if tool_name:
                await rc.record_tool_call(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    result=result_map.get(call_id, ""),
                )

        await rc.set(self._PENDING_KEY, [])
