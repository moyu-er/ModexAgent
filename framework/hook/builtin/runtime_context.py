"""RuntimeContextHook — 管理 per-turn RuntimeContext 生命周期。

- before_turn：解析并缓存 session 的 RuntimeContext，为新 turn 清空
- before_tool_execution：缓存待执行的工具调用列表
- after_tool_execution：将工具调用结果录入 ToolCallRecord
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext

from framework.core.agent import ctx_ext
from framework.core.context_extensions import ExtensionKey


class RuntimeContextHook:
    """通过 hook 接口管理 per-turn RuntimeContext 生命周期。"""

    _PENDING_KEY = "_pending_tool_calls"

    async def before_turn(self, ctx: AgentContext[Any]) -> None:
        rt_mgr = ctx_ext(ctx, ExtensionKey.RUNTIME_CTX_MGR)
        rc = ctx_ext(ctx, ExtensionKey.RUNTIME_CTX)
        if rc is None and rt_mgr is not None:
            rc = await rt_mgr.get_context(
                ctx.session_id, ctx.metadata
            )
            ctx.extensions[ExtensionKey.RUNTIME_CTX] = rc
        if rc is not None:
            await rc.clear()

    async def before_tool_execution(
        self,
        ctx: AgentContext[Any],
        tool_calls: list[Any] | None = None,
    ) -> None:
        rc = ctx_ext(ctx, ExtensionKey.RUNTIME_CTX)
        if rc is not None and tool_calls:
            await rc.set(self._PENDING_KEY, list(tool_calls))

    async def after_tool_execution(
        self,
        ctx: AgentContext[Any],
        results: list[Any] | None = None,
    ) -> None:
        rc = ctx_ext(ctx, ExtensionKey.RUNTIME_CTX)
        if rc is None or results is None:
            return
        pending = await rc.get(self._PENDING_KEY, [])
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
                await rc.record_tool_call(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    result=result_map.get(call_id, ""),
                )

        await rc.set(self._PENDING_KEY, [])
