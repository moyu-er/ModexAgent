from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import MessageRole
from modex_agent.interceptor.abc import (
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
)
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 50_000


class ToolResultLimitInterceptor(ToolCallInterceptor):
    """Tool result overflow interceptor.

    When a tool result exceeds *max_chars*, the full content is persisted
    to disk and the model receives truncated text with a path to the full
    output. Falls back to simple truncation when *overflow_handler* is None.
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

    def repoint_overflow_store(self, store: object) -> None:
        """Retarget the overflow handler's store (workspace switch).

        No-op when this interceptor has no overflow handler installed.
        """
        if self._handler is not None:
            self._handler.repoint_store(store)

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        result: ToolResult = await next_call()  # type: ignore[misc]

        # 1. Skip if already processed, has error, or no content
        if result.error or not result.content or result.overflow_processed:
            return result

        # 2. Short result — pass through
        result_str = result.message_content()
        if len(result_str) <= self._max_chars:
            return result

        # 3. No handler — fallback to old truncation
        if self._handler is None:
            truncated = result_str[: self._max_chars] + (
                f"\n... (truncated, {len(result_str)} chars total)"
            )
            return ToolResult.from_text(
                result.tool_name,
                truncated,
                call_id=result.call_id,
                execution_time=result.execution_time,
                overflow_processed=False,
            )

        # 4. Overflow path — write to disk synchronously, then return.
        #    Cleanup is fire-and-forget; must not block the agent turn.
        session_id = self._get_session_id(ctx)
        tool_call_id = call.tool_call.call_id or f"{call.tool_name}-{uuid4().hex[:12]}"

        try:
            overflow_content, _ref = await self._handler.store_overflow(
                session_id=session_id,
                tool_call_id=tool_call_id,
                tool_name=call.tool_name,
                content=result_str,
                max_chars=self._max_chars,
            )
        except Exception:
            logger.exception("Overflow store failed for %s/%s", session_id, tool_call_id)
            return ToolResult.from_text(
                result.tool_name,
                result_str[: self._max_chars],
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

        return ToolResult.from_text(
            result.tool_name,
            overflow_content,
            call_id=result.call_id,
            overflow_processed=True,
        )

    @staticmethod
    def _default_session_id(ctx: AgentContext) -> str:
        return str(ctx.session) or "default"

    @staticmethod
    async def _gather_kept_call_ids(ctx: AgentContext) -> set[str]:
        call_ids: set[str] = set()
        try:
            messages = await ctx.history.to_list()
            for msg in messages:
                if msg.role != MessageRole.TOOL.value:
                    continue
                tc_id = msg.tool_call_id
                if tc_id:
                    call_ids.add(tc_id)
        except Exception:
            logger.warning("Failed to gather kept call_ids from history", exc_info=True)
        return call_ids
