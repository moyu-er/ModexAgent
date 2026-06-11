"""TraceCollectorHook — lifecycle hook that records per-operation traces."""

from __future__ import annotations

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


class TraceCollectorHook(
    BeforeTurnHook[None],
    AfterLLMResponseHook[None],
    BeforeToolExecutionHook[None],
    AfterToolExecutionHook[None],
    FinallyTurnHook[None],
):
    """Collects operation traces at each lifecycle hook point.

    Records TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, and TURN_END
    events into the configured TraceStore.
    """

    def __init__(self, store: TraceStore, *, enabled: bool = True) -> None:
        self._store = store
        self._enabled = enabled

    @property
    def name(self) -> str:
        return "trace_collector"

    # -- helpers -------------------------------------------------------------

    def _trace_id(self, ctx: AgentContext[None]) -> str:
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
        """Persist a record, logging failures instead of raising."""
        try:
            await self._store.save(rec)
        except Exception:
            logger.warning("TraceCollectorHook failed to save record", exc_info=True)

    def _agent_name(self, ctx: AgentContext[None]) -> str:
        """Return agent name from session_meta or identity."""
        if ctx.session_meta is not None and ctx.session_meta.agent_name:
            return ctx.session_meta.agent_name
        if ctx.identity is not None:
            return ctx.identity.agent_id
        return "unknown"

    def _invocation_id(self, ctx: AgentContext[None]) -> str | None:
        if ctx.session_meta is not None:
            return ctx.session_meta.invocation_id
        return None

    # -- hook implementations ------------------------------------------------

    async def before_turn(self, ctx: AgentContext[None]) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TURN_START,
            status=OperationStatus.COMPLETED,
            timestamp=time.time(),
            metadata={"turn_id": ctx.identity.turn_id if ctx.identity else None},
        )
        await self._save(rec)

    async def after_llm_response(
        self, ctx: AgentContext[None], response: LLMResponse
    ) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        metadata: dict = {
            "finish_reason": response.finish_reason,
            "has_tool_calls": response.has_tool_calls,
        }
        if response.usage:
            metadata["usage"] = response.usage
        if response.tool_calls:
            metadata["tool_call_names"] = [tc.tool_name for tc in response.tool_calls]
        status = OperationStatus.FAILED if response.error else OperationStatus.COMPLETED
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=ctx.session_id,
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
        self, ctx: AgentContext[None], tool_calls: Sequence[ToolCall]
    ) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TOOL_BATCH,
            status=OperationStatus.RUNNING,
            timestamp=time.time(),
            metadata={
                "tool_count": len(tool_calls),
                "tool_names": [tc.tool_name for tc in tool_calls],
            },
        )
        await self._save(rec)

    async def after_tool_execution(
        self, ctx: AgentContext[None], results: Sequence[ToolResult]
    ) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        for result in results:
            metadata: dict = {"tool_name": result.tool_name}
            if result.execution_time is not None:
                metadata["duration_ms"] = int(result.execution_time * 1000)
            status = OperationStatus.FAILED if result.error else OperationStatus.COMPLETED
            rec = OperationRecord(
                trace_id=trace_id,
                session_id=ctx.session_id,
                agent_name=self._agent_name(ctx),
                invocation_id=self._invocation_id(ctx),
                kind=OperationKind.TOOL_CALL,
                status=status,
                timestamp=time.time(),
                metadata=metadata,
                error=result.error,
            )
            await self._save(rec)

    async def finally_turn(
        self, ctx: AgentContext[None], result: AgentResult | None
    ) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        if result is not None and result.error:
            status = OperationStatus.FAILED
            error = result.error
        else:
            status = OperationStatus.COMPLETED
            error = None
        metadata: dict = {}
        if result is not None:
            metadata["stop_reason"] = str(result.stop_reason)
            if result.content is not None:
                metadata["content_length"] = len(result.content)
        rec = OperationRecord(
            trace_id=trace_id,
            session_id=ctx.session_id,
            agent_name=self._agent_name(ctx),
            invocation_id=self._invocation_id(ctx),
            kind=OperationKind.TURN_END,
            status=status,
            timestamp=time.time(),
            metadata=metadata,
            error=error,
        )
        await self._save(rec)
