"""ToolTimeoutInterceptor — 工具调用超时拦截器。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.tool_manager import ToolResult

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT = 180.0


class ToolTimeoutInterceptor:
    """工具调用超时拦截器。

    读取 ctx.runtime.safety.turn.tool_timeout_seconds 作为默认超时值。
    只有当前边界没有更高层 timeout owner 时才生效。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(self, timeout_seconds: float | None = None) -> None:
        self._timeout = timeout_seconds

    async def around_tool_call(
        self,
        ctx: AgentContext[Any],
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        from framework.core.tool_manager import ToolResult

        timeout = self._resolve_timeout(ctx)

        try:
            result = await asyncio.wait_for(next_call(), timeout=timeout)
            return result
        except TimeoutError:
            logger.warning(
                "Tool %s timed out after %.1fs (interceptor)",
                call.tool_name,
                timeout,
            )
            return ToolResult(
                tool_name=call.tool_name,
                result=None,
                error=f"Error: Tool execution timeout after {timeout:.0f}s",
            )

    def _resolve_timeout(self, ctx: AgentContext[Any]) -> float:
        if self._timeout is not None:
            return self._timeout
        if ctx.runtime and ctx.runtime.safety:
            return ctx.runtime.safety.turn.tool_timeout_seconds
        return _DEFAULT_TOOL_TIMEOUT
