from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from framework.core.tool_manager import ToolResult
from framework.interceptor.abc import (
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
)
from framework.tools.overflow.handler import ToolResultOverflowHandler

if TYPE_CHECKING:
    from framework.core.agent import AgentContext

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 50_000


class ToolResultLimitInterceptor(ToolCallInterceptor):
    """Tool result overflow interceptor.

    When a tool result exceeds *max_chars*, the full content is persisted
    to disk and the model receives a short ``[TOOL_RESULT_TRUNCATED]``
    notice telling it where to find the complete chunks.  Falls back to
    truncation when *overflow_handler* is None.
    """

    @property
    def name(self) -> str:
        return "tool_result_limit"

    def __init__(
        self,
        overflow_handler: ToolResultOverflowHandler | None = None,
        max_chars: int = _DEFAULT_MAX_CHARS,
        session_id_provider: Callable[[AgentContext], str] | None = None,
    ) -> None:
        self._handler = overflow_handler
        self._max_chars = max_chars
        self._get_session_id = session_id_provider or self._default_session_id

    @property
    def handler(self) -> ToolResultOverflowHandler | None:
        return self._handler

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

        # 4. Overflow path — write to disk synchronously, then return.
        #    Cleanup is fire-and-forget; must not block the agent turn.
        session_id = self._get_session_id(ctx)
        tool_call_id = call.tool_call.call_id or f"{call.tool_name}-{uuid4().hex[:12]}"

        try:
            chunk_1_content, _ref = await self._handler.store_overflow(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=call.tool_name,
                content=result_str,
            )
        except Exception:
            logger.exception("Overflow store failed for %s/%s", session_id, tool_call_id)
            return ToolResult(
                tool_name=result.tool_name,
                result=result_str[:self._max_chars],
                call_id=result.call_id,
                overflow_processed=False,
            )

        # 5. Schedule async cleanup — after the tool result has been
        #    written to session history by the caller (ReAct agent).
        try:
            kept_call_ids = await self._gather_kept_call_ids(ctx)
        except Exception:
            logger.debug("Failed to gather kept call_ids, keeping current only", exc_info=True)
            kept_call_ids = set()
        kept_call_ids.add(tool_call_id)
        self._handler.schedule_cleanup(session_id, kept_call_ids)

        return ToolResult(
            tool_name=result.tool_name,
            result=chunk_1_content,
            call_id=result.call_id,
            overflow_processed=True,
        )

    @staticmethod
    def _default_session_id(ctx: AgentContext) -> str:
        return ctx.session_id or "default"

    @staticmethod
    async def _gather_kept_call_ids(ctx: AgentContext) -> set[str]:
        call_ids: set[str] = set()
        try:
            messages = await ctx.history.to_list()
            for msg in messages:
                if msg.role != "tool":
                    continue
                if msg.tool_call_id:
                    call_ids.add(msg.tool_call_id)
        except Exception:
            logger.warning("Failed to gather kept call_ids from history", exc_info=True)
        return call_ids
