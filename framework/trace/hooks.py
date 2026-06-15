"""TraceCollectorHook — lifecycle hook that records per-operation traces."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from framework.hook.abc import (
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinallyTurnHook,
)
from framework.runtime.enums import OperationKind, OperationStatus, TurnCustomKey
from framework.trace.store import TraceStore
from framework.trace.types import OperationRecord

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult
    from framework.core.tool_manager import ToolResult
    from framework.core.types import LLMResponse, ToolCall

logger = logging.getLogger(__name__)

# Character limit for content stored in trace metadata (file-friendly).
# Full content should be reported to server backends; this is a local cap.
_CONTENT_MAX_CHARS = 4000
_ARG_MAX_CHARS = 2000


def _truncate(text: str, max_chars: int = _CONTENT_MAX_CHARS) -> str:
    """Return *text* truncated to *max_chars* with a truncation marker."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[...truncated, {len(text) - max_chars} more chars]"


def _safe_json_dumps(obj: object, max_chars: int = _ARG_MAX_CHARS) -> str:
    """JSON-serialise *obj* and truncate if needed."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return _truncate(s, max_chars)


class TraceCollectorHook(
    BeforeTurnHook,
    AfterLLMResponseHook,
    BeforeToolExecutionHook,
    AfterToolExecutionHook,
    FinallyTurnHook,
):
    """Collects operation traces at each lifecycle hook point.

    Records TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, and TURN_END
    events with full message content (truncated for local file storage)
    into one or more configured TraceStores.
    """

    def __init__(
        self,
        store: TraceStore | None = None,
        stores: Sequence[TraceStore] | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._stores: Sequence[TraceStore] = (
            list(stores) if stores else ([store] if store else [])
        )
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "trace_collector"

    # -- helpers -------------------------------------------------------------

    def _trace_id(self, ctx: AgentContext) -> str:
        """Return existing trace_id from turn state or generate a new one."""
        if ctx.runtime is None:
            return uuid.uuid4().hex
        tid = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        if tid is not None:
            return str(tid)
        new_id = uuid.uuid4().hex
        ctx.runtime.state.custom[TurnCustomKey.TRACE_ID] = new_id
        return new_id

    async def _save(self, rec: OperationRecord) -> None:
        """Persist a record to all configured stores, logging failures."""
        for s in self._stores:
            try:
                await s.save(rec)
            except Exception:
                logger.warning(
                    "TraceCollectorHook failed to save to %s",
                    type(s).__name__,
                    exc_info=True,
                )

    def _agent_name(self, ctx: AgentContext) -> str:
        """Return agent name from session."""
        return ctx.session.agent_name if ctx.session else "unknown"

    def _invocation_id(self, ctx: AgentContext) -> str | None:
        """Return invocation_id from session metadata."""
        if ctx.session is not None:
            return str(ctx.session.metadata.get("invocation_id", "")) or None
        return None

    async def _last_user_messages(self, ctx: AgentContext, limit: int = 3) -> list[dict[str, object]]:
        """Return the last *limit* user/assistant messages from history for context."""
        try:
            all_msgs = await ctx.history.to_list()
        except Exception:
            return []
        recent: list[dict[str, object]] = []
        for msg in reversed(all_msgs[-20:]):  # check last 20, take up to limit
            role = msg.get("role", "unknown") if isinstance(msg, dict) else getattr(msg, "role", "unknown")
            if role in ("user", "assistant"):
                content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
                recent.append({
                    "role": role,
                    "content": _truncate(str(content)[:2000], 2000),
                })
                if len(recent) >= limit:
                    break
        recent.reverse()
        return recent

    # -- hook implementations ------------------------------------------------

    async def before_turn(self, ctx: AgentContext) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        metadata: dict[str, object] = {
            "turn_id": ctx.identity.turn_id if ctx.identity else None,
            "recent_messages": await self._last_user_messages(ctx),
        }
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=str(ctx.session),
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TURN_START,
            status=OperationStatus.COMPLETED,
            timestamp=time.time(),
            metadata=metadata,
        )
        await self._save(rec)

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        metadata: dict[str, object] = {
            "finish_reason": response.finish_reason,
            "has_tool_calls": response.has_tool_calls,
            "content": _truncate(response.content or ""),
        }
        if response.reasoning_content:
            metadata["reasoning"] = _truncate(response.reasoning_content)
        if response.usage:
            metadata["usage"] = response.usage
        if response.tool_calls:
            metadata["tool_calls"] = [
                {
                    "tool_name": tc.tool_name,
                    "arguments": _safe_json_dumps(tc.arguments),
                }
                for tc in response.tool_calls
            ]
        status = OperationStatus.FAILED if response.error else OperationStatus.COMPLETED
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=str(ctx.session),
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.LLM_CALL,
            status=status,
            timestamp=time.time(),
            metadata=metadata,
            error=response.error,
        )
        await self._save(rec)

    async def before_tool_execution(
        self, ctx: AgentContext, tool_calls: Sequence[ToolCall]
    ) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=str(ctx.session),
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TOOL_BATCH,
            status=OperationStatus.RUNNING,
            timestamp=time.time(),
            metadata={
                "tool_count": len(tool_calls),
                "tool_names": [tc.tool_name for tc in tool_calls],
                "tool_arguments": [
                    {"tool_name": tc.tool_name, "arguments": _safe_json_dumps(tc.arguments)}
                    for tc in tool_calls
                ],
            },
        )
        await self._save(rec)

    async def after_tool_execution(
        self, ctx: AgentContext, results: Sequence[ToolResult]
    ) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        for result in results:
            metadata: dict[str, object] = {"tool_name": result.tool_name}
            if result.execution_time is not None:
                metadata["duration_ms"] = int(result.execution_time * 1000)
            if result.result is not None:
                metadata["result"] = _truncate(str(result.result))
            status = OperationStatus.FAILED if result.error else OperationStatus.COMPLETED
            rec = OperationRecord(
                trace_id=trace_id,
                session_id=str(ctx.session),
                agent_name=self._agent_name(ctx),
                invocation_id=self._invocation_id(ctx),
                kind=OperationKind.TOOL_CALL,
                status=status,
                timestamp=time.time(),
                metadata=metadata,
                error=result.error,
            )
            await self._save(rec)

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        if result is not None and result.error:
            status = OperationStatus.FAILED
            error = result.error
        else:
            status = OperationStatus.COMPLETED
            error = None
        metadata: dict[str, object] = {}
        if result is not None:
            metadata["stop_reason"] = str(result.stop_reason)
            if result.content is not None:
                metadata["content"] = _truncate(result.content)
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=str(ctx.session),
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TURN_END,
            status=status,
            timestamp=time.time(),
            metadata=metadata,
            error=error,
        )
        await self._save(rec)
