"""ToolResultLimitInterceptor — 工具结果长度限制拦截器。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import ToolResult

_DEFAULT_MAX_CHARS = 4000


class ToolResultLimitInterceptor:
    """工具结果长度限制拦截器。

    截断超长的工具结果，保留完整的 tool_call_id 和错误标记。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(self, max_chars: int = _DEFAULT_MAX_CHARS) -> None:
        self._max_chars = max_chars

    async def around_tool_call(
        self,
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        result = await next_call()

        if result.error or result.result is None:
            return result

        result_str = str(result.result)
        if len(result_str) <= self._max_chars:
            return result

        from framework.core.tool_manager import ToolResult

        truncated = result_str[:self._max_chars] + (
            f"\n... (truncated, {len(result_str)} chars total)"
        )
        return ToolResult(
            tool_name=result.tool_name,
            result=truncated,
            call_id=getattr(result, "call_id", None),
        )
