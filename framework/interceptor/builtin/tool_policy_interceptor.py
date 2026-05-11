"""ToolPolicyInterceptor — 策略静默否决不合规 tool_call。

插件或自定义 hook 可在 before_tool_execution 中将 runtime state 的
``_policy_denied_tools`` 设置为 {tool_name: reason} 字典。
本拦截器在 around_tool_call 中检查该标记，匹配时返回伪 ToolResult 阻止实际执行。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)
from framework.runtime.enums import TurnCustomKey

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import ToolResult

logger = logging.getLogger(__name__)


class ToolPolicyInterceptor:
    """策略静默否决拦截器。

    在 TOOL_CALL scope 中检查 ctx.metadata["_policy_denied_tools"]，
    匹配时返回伪 ToolResult(error=...) 而非执行实际工具。

    必须注册在 ToolWatchInterceptor 外层
    （洋葱序上靠前），让策略否决短路其他拦截器。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    async def around_tool_call(
        self,
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        state = ctx.runtime.state if ctx.runtime else None
        denied: dict[str, str] | None = state.custom.get(TurnCustomKey.POLICY_DENIED_TOOLS) if state else None
        if denied and call.tool_name in denied:
            from framework.core.tool_manager import ToolResult

            reason = denied[call.tool_name]
            logger.info(
                "ToolPolicyInterceptor: vetoed tool=%s reason=%s session=%s",
                call.tool_name, reason, ctx.session_id,
            )
            call_id = call.tool_call.call_id or "" if call.tool_call else ""
            return ToolResult(
                tool_name=call.tool_name,
                call_id=call_id,
                result=None,
                error=f"Error: Tool '{call.tool_name}' is blocked by policy: {reason}",
            )
        return await next_call()
