"""RuntimeContextHook — 管理 per-turn RuntimeContext 生命周期。

- before_turn：解析并缓存 session 的 RuntimeContext，为新 turn 清空
- before_tool_execution：缓存待执行的工具调用列表
- after_tool_execution：将工具调用结果录入 ToolCallRecord
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


class RuntimeContextHook:
    """通过 hook 接口管理 per-turn RuntimeContext 生命周期。"""

    _PENDING_KEY = "_pending_tool_calls"

    async def before_turn(self, ctx: AgentContext) -> None:
        if ctx.runtime_context is None and ctx.runtime_context_manager is not None:
            ctx.runtime_context = await ctx.runtime_context_manager.get_context(
                ctx.session_id, ctx.metadata
            )
        if ctx.runtime_context is not None:
            await ctx.runtime_context.clear()

    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: list[Any] | None = None,
    ) -> None:
        if ctx.runtime_context is not None and tool_calls:
            await ctx.runtime_context.set(self._PENDING_KEY, list(tool_calls))

    async def after_tool_execution(
        self,
        ctx: AgentContext,
        results: list[Any] | None = None,
    ) -> None:
        if ctx.runtime_context is None or results is None:
            return
        pending = await ctx.runtime_context.get(self._PENDING_KEY, [])
        if not pending:
            return

        result_map: dict[str | None, str] = {
            msg.get("tool_call_id"): msg.get("content", "")
            for msg in results
            if isinstance(msg, dict) and msg.get("role") == "tool"
        }

        for tool_call in pending:
            call_id = getattr(tool_call, "call_id", None)
            tool_name = getattr(tool_call, "tool_name", None)
            arguments = getattr(tool_call, "arguments", None) or {}
            if tool_name:
                await ctx.runtime_context.record_tool_call(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    result=result_map.get(call_id, ""),
                )

        await ctx.runtime_context.set(self._PENDING_KEY, [])
