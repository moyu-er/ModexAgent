from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING
from uuid import uuid4

from modex_agent.core.message import TextPart
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
    from modex_agent.tools.overflow.store import ToolOverflowStore

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

    def repoint_overflow_store(self, store: ToolOverflowStore) -> None:
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
            return self._rebuild_truncated(result, self._render_unpersisted(result_str))

        # 4. Overflow path. Ordering closes the cleanup race: the kept-set
        #    must cover this call BEFORE its entry lands on disk. A clean
        #    pass firing while a just-stored entry is not yet in any
        #    scheduled kept-set would delete it (the entry is on disk but
        #    absent from session history — indistinguishable from stale).
        session_id = self._get_session_id(ctx)
        tool_call_id = call.tool_call.call_id or f"{call.tool_name}-{uuid4().hex[:12]}"

        try:
            kept_call_ids = await self._gather_kept_call_ids(ctx)
        except Exception:
            logger.debug("Failed to gather kept call_ids, keeping current only", exc_info=True)
            kept_call_ids = set()
        kept_call_ids.add(tool_call_id)
        self._handler.schedule_cleanup(session_id, kept_call_ids)

        # 5. Write to disk synchronously, then return. Cleanup is
        #    fire-and-forget; must not block the agent turn.
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
            return self._rebuild_truncated(result, self._render_unpersisted(result_str))

        return self._rebuild_truncated(result, overflow_content, overflow_processed=True)

    def _rebuild_truncated(
        self,
        result: ToolResult,
        truncated_text: str,
        *,
        overflow_processed: bool = False,
    ) -> ToolResult:
        """Rebuild a ToolResult with truncated text, preserving siblings.

        ``model_copy`` keeps every field the tool set — ``content_format`` and
        ``truncatable_paths`` (governance truncation metadata declared by
        terminal tools) and non-text content parts (e.g. ImageUrlPart) — so a
        text overflow never silently drops metadata or multimodal content.
        Text parts are replaced by the single truncated text.
        """
        non_text = [part for part in result.content if not isinstance(part, TextPart)]
        return result.model_copy(
            update={
                "content": [TextPart(text=truncated_text), *non_text],
                "overflow_processed": overflow_processed,
            }
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
