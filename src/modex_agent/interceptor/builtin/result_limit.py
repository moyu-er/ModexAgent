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
from modex_agent.tools.overflow.truncate import (
    DEFAULT_HEAD_RATIO,
    DEFAULT_TAIL_RATIO,
    render_overflow_text,
    split_head_tail,
)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 50_000


class ToolResultLimitInterceptor(ToolCallInterceptor):
    """Tool result overflow interceptor.

    When a tool result exceeds *max_chars*, the full content is persisted
    to disk and the model receives head + elision marker + tail + a path to
    the full output. Error results are bounded too — errors, stack traces,
    and exit codes cluster at the END of tool output. Falls back to the same
    head/tail shape without a persisted path when *overflow_handler* is None
    or storing fails.
    """

    @property
    def name(self) -> str:
        return "tool_result_limit"

    def __init__(
        self,
        overflow_handler: ToolResultOverflowHandler | None = None,
        max_chars: int = _DEFAULT_MAX_CHARS,
        session_id_provider: Callable[[AgentContext], str] | None = None,
        head_ratio: float = DEFAULT_HEAD_RATIO,
        tail_ratio: float = DEFAULT_TAIL_RATIO,
    ) -> None:
        self._handler = overflow_handler
        self._max_chars = max_chars
        self._get_session_id = session_id_provider or self._default_session_id
        self._head_ratio = head_ratio
        self._tail_ratio = tail_ratio

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

        # 1. Skip if already processed
        if result.overflow_processed:
            return result

        # 2. Short result — pass through. Error results are NOT exempt:
        #    bounding only shrinks the rendered content, the error flag is
        #    preserved on the rebuilt ToolResult.
        result_str = result.message_content()
        if not result_str or len(result_str) <= self._max_chars:
            return result

        # 3. No handler — head/tail truncation, full output NOT persisted
        if self._handler is None:
            return ToolResult.from_text(
                result.tool_name,
                self._render_unpersisted(result_str),
                call_id=result.call_id,
                execution_time=result.execution_time,
                error=result.error,
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
                self._render_unpersisted(result_str),
                call_id=result.call_id,
                error=result.error,
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
            error=result.error,
            overflow_processed=True,
        )

    def _render_unpersisted(self, content: str) -> str:
        """Head/tail truncation for paths where the full output is NOT persisted."""
        head_chars, tail_chars = split_head_tail(
            self._max_chars, self._head_ratio, self._tail_ratio
        )
        return render_overflow_text(
            content,
            head_chars=head_chars,
            tail_chars=tail_chars,
            full_output_path=None,
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
