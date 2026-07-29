"""ToolExecutor — run a tool call through the interceptor chain.

The mandatory ``ToolTimeoutInterceptor`` is composed as the innermost
interceptor, ensuring every ReAct path (clean, full, main, subagent) has
a per-invocation tool deadline without relying on application-level
interceptor registration.
"""

from __future__ import annotations

from modex_agent.core.agent import AgentContext
from modex_agent.core.tool_manager import ToolExecutionContext, ToolResult
from modex_agent.core.types import ToolCall
from modex_agent.interceptor.abc import ToolCallContext
from modex_agent.interceptor.builtin.tool_timeout import ToolTimeoutInterceptor
from modex_agent.workspace.runtime import resolve_workspace_root


class ToolExecutor:
    """Execute a single ToolCall via the interceptor chain.

    The ``ToolTimeoutInterceptor`` always wraps the actual tool execution,
    whether or not an application-level interceptor chain exists.
    """

    def __init__(self) -> None:
        self._timeout_interceptor = ToolTimeoutInterceptor()

    async def execute(self, tool_call: ToolCall, ctx: AgentContext) -> ToolResult:
        call_ctx = ToolCallContext(
            tool_call=tool_call,
            tool_name=tool_call.tool_name,
            arguments=tool_call.arguments or {},
            session_id=str(ctx.session),
        )

        caps = (
            ctx.runtime.model_capabilities
            if ctx.runtime is not None
            else None
        )
        ws_root = resolve_workspace_root()
        tool_exec_ctx = ToolExecutionContext(
            model_capabilities=caps,
            workspace_root=ws_root,
            tool_call_id=tool_call.call_id,
            session_id=str(ctx.session),
        )

        async def _actual() -> ToolResult:
            return await ctx.tool_manager.execute(
                tool_call.tool_name, tool_call.arguments or {}, ctx=tool_exec_ctx
            )

        async def _timed() -> ToolResult:
            return await self._timeout_interceptor.around_tool_call(
                ctx, call_ctx, _actual  # type: ignore[misc]
            )

        interceptor_chain = ctx.runtime.interceptors if ctx.runtime else None
        if interceptor_chain is not None:
            return await interceptor_chain.around_tool_call(
                ctx, call_ctx, _timed  # type: ignore[misc]
            )
        return await _timed()
