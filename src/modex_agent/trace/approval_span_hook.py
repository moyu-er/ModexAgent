"""ApprovalSpanHook -- emits the ``human.review`` span.

On AFTER_APPROVAL it emits a single INTERNAL EVENT span parented to the turn
root span, recording the decision, deny reason (when denied), and the tool
that triggered the approval.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from modex_agent.approval.constants import ApprovalStatus
from modex_agent.hook.abc import AfterApprovalHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.runtime.models import ApprovalTransaction
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationLevel,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanStatus

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext


class ApprovalSpanHook(BaseTraceHook, AfterApprovalHook):
    """Emit a ``human.review`` EVENT span after each approval decision."""

    async def after_approval(
        self,
        ctx: AgentContext,
        transaction: ApprovalTransaction,
    ) -> None:
        if ctx.runtime is None:
            return
        trace_value = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        root_span_id = str(root_value)
        now = time.time()
        denied = transaction.status == ApprovalStatus.DENIED
        attrs = self._build_base_attrs(ctx, SpanName.HUMAN_REVIEW.value)
        attrs[GenAiAttr.APPROVAL_DECISION] = str(transaction.status)
        attrs[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] = LangfuseObservationType.EVENT.value
        attrs[GenAiAttr.LANGFUSE_OBSERVATION_LEVEL] = (
            LangfuseObservationLevel.WARNING.value
            if denied
            else LangfuseObservationLevel.DEFAULT.value
        )
        if transaction.deny_reason is not None:
            attrs[GenAiAttr.APPROVAL_DENY_REASON] = transaction.deny_reason
        if transaction.requests:
            req = transaction.requests[0]
            attrs[GenAiAttr.APPROVAL_TOOL_NAME] = req.tool_name
            attrs[GenAiAttr.APPROVAL_TOOL_CALL_ID] = req.tool_call_id
        await self._save_span(
            trace_id=trace_id,
            span_id=self._new_span_id(),
            parent_span_id=root_span_id,
            name=SpanName.HUMAN_REVIEW.value,
            kind=SpanKind.INTERNAL.value,
            start_time=now,
            end_time=now,
            attributes=attrs,
            status=SpanStatus(code=SpanStatusCode.OK),
            ctx=ctx,
        )
