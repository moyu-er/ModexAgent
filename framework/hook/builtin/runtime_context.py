"""RuntimeContextHook — 管理 per-turn RuntimeContext 生命周期。

- before_turn：解析并缓存 session 的 RuntimeContext，为新 turn 清空
- before_tool_execution：缓存待执行的工具调用列表
- after_tool_execution：将工具调用结果录入 ToolCallRecord
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.core.session_id import SessionId
from framework.core.tool_manager import ToolResult
from framework.hook.abc import AfterToolExecutionHook, BeforeToolExecutionHook, BeforeTurnHook

if TYPE_CHECKING:
    from framework.core.agent import AgentContext


class RuntimeContextHook(BeforeTurnHook, BeforeToolExecutionHook, AfterToolExecutionHook):
    """通过 hook 接口管理 per-turn RuntimeContext 生命周期。"""

    @property
    def name(self) -> str:
        return "runtime_context_hook"

    _PENDING_KEY = "_pending_tool_calls"

    async def before_turn(self, ctx: AgentContext) -> None:
        rt = ctx.runtime
        if rt is None:
            return
        rt_mgr = rt.services.runtime_context_manager
        if rt_mgr is not None and rt._runtime_context is None:
            session = SessionId.from_str(
                str(ctx.session),
                default_agent_name=ctx.session.agent_name,
            )
            rt._runtime_context = await rt_mgr.get_context(session, None)
        rc = rt._runtime_context
        if rc is not None:
            await rc.clear()

    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: list[Any] | None = None,
    ) -> None:
        if ctx.runtime is None:
            return
        rc = ctx.runtime._runtime_context
        if rc is not None and tool_calls:
            await rc.set(self._PENDING_KEY, list(tool_calls))

    async def after_tool_execution(
        self,
        ctx: AgentContext,
        results: list[Any] | None = None,
    ) -> None:
        if ctx.runtime is None:
            return
        rc = ctx.runtime._runtime_context
        if rc is None:
            return
        pending = await rc.get(self._PENDING_KEY, [])
        if not pending:
            return

        result_map: dict[str | None, str] = {}
        if results:
            for r in results:
                if isinstance(r, ToolResult):
                    result_map[r.call_id] = f"Error: {r.error}" if r.error else str(r.result)
                elif isinstance(r, dict) and r.get("role") == "tool":
                    result_map[r.get("tool_call_id")] = r.get("content", "")

        for tool_call in pending:
            call_id = getattr(tool_call, "call_id", None)
            tool_name = getattr(tool_call, "tool_name", None)
            arguments = getattr(tool_call, "arguments", None) or {}
            if tool_name:
                await rc.record_tool_call(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    result=result_map.get(call_id, ""),
                )

        await rc.set(self._PENDING_KEY, [])
