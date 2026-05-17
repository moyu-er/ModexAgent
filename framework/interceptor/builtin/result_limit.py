from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from framework.core.tool_manager import ToolResult
from framework.interceptor.abc import (
    InterceptorScope,
    ToolCallContext,
    ToolCallNext,
)
from framework.tools.overflow.handler import ToolResultOverflowHandler

if TYPE_CHECKING:
    from framework.core.agent import AgentContext

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 10000


class ToolResultLimitInterceptor:
    """工具结果溢出拦截器。

    超限结果写入 ToolOverflowStore，模型收到第 1 块完整内容 +
    [TOOL_RESULT_OVERFLOW] 前缀（含路径和序号信息）。

    当 overflow_handler 为 None 时回退到旧截断行为。
    """

    scopes = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(
        self,
        overflow_handler: ToolResultOverflowHandler | None = None,
        max_chars: int = _DEFAULT_MAX_CHARS,
        session_id_provider: Callable[[AgentContext], str] | None = None,
    ) -> None:
        self._handler = overflow_handler
        self._max_chars = max_chars
        self._get_session_id = session_id_provider or self._default_session_id

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        result: ToolResult = await next_call()  # type: ignore[misc]

        # 1. Skip if already processed, has error, or no result
        if result.error or result.result is None or result.overflow_processed:
            return result

        # 2. Short result — pass through
        result_str = str(result.result)
        if len(result_str) <= self._max_chars:
            return result

        # 3. No handler — fallback to old truncation
        if self._handler is None:
            truncated = result_str[:self._max_chars] + (
                f"\n... (truncated, {len(result_str)} chars total)"
            )
            return ToolResult(
                tool_name=result.tool_name,
                result=truncated,
                error=result.error,
                execution_time=result.execution_time,
                call_id=result.call_id,
                overflow_processed=False,
            )

        # 4. Overflow path
        session_id = self._get_session_id(ctx)
        tool_call_id = call.tool_call.id if hasattr(call.tool_call, "id") else call.tool_name

        chunk_1_content, _ref = await self._handler.store_overflow(
            session_id=session_id,
            tool_call_id=tool_call_id,
            tool_name=call.tool_name,
            content=result_str,
        )

        # 5. Trigger async cleanup
        kept_call_ids = self._gather_kept_call_ids(ctx)
        self._handler.schedule_cleanup(session_id, kept_call_ids)

        return ToolResult(
            tool_name=result.tool_name,
            result=chunk_1_content,
            call_id=getattr(result, "call_id", None),
            overflow_processed=True,
        )

    @staticmethod
    def _default_session_id(ctx: AgentContext) -> str:
        return ctx.session_id or "default"

    @staticmethod
    def _gather_kept_call_ids(ctx: AgentContext) -> set[str]:
        call_ids: set[str] = set()
        try:
            messages: Any = getattr(ctx.history, "messages", [])
            for msg in messages:
                role = msg.get("role", "") if isinstance(msg, dict) else getattr(msg, "role", "")
                if role != "tool":
                    continue
                tc_id = msg.get("tool_call_id", "") if isinstance(msg, dict) else getattr(msg, "tool_call_id", "")
                if tc_id:
                    call_ids.add(str(tc_id))
        except Exception:
            logger.warning("Failed to gather kept call_ids from history", exc_info=True)
        return call_ids
