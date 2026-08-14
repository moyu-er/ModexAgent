"""IterationSpanHook -- emits iteration.start / iteration.end spans.

Emits a symmetric pair of INTERNAL spans per ReAct loop iteration, both
parented to the turn root span:

- ``iteration.start`` (BEFORE_ITERATION) -- marks iteration begin.
- ``iteration.end`` (AFTER_ITERATION) -- marks iteration end, carrying the
  measured duration.

Symmetry: every ``before_iteration`` that fires with an active trace gets a
matching ``after_iteration``. The old ``iteration_number -= 1`` hack is
removed -- with AFTER_ITERATION now firing at current-iteration-end (T16),
``state.iteration`` is still the current value, so no decrement is needed.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.hook.abc import AfterIterationHook, BeforeIterationHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanStatus

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext


class IterationSpanHook(BaseTraceHook, BeforeIterationHook, AfterIterationHook):
    """Emit ``iteration.start`` / ``iteration.end`` spans per ReAct iteration."""

    async def before_iteration(self, ctx: AgentContext) -> None:
        if ctx.runtime is None:
            return
        state = ctx.runtime.state
        if not isinstance(state, ReActTurnState):
            return
        trace_value = state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        root_span_id = str(root_value)
        iteration_number = state.iteration
        now = time.time()
        self._session.iteration_start_times[trace_id] = now

        attrs = self._build_base_attrs(ctx, "")
        attrs.pop(GenAiAttr.OPERATION_NAME, None)
        attrs[GenAiAttr.ITERATION_NUMBER] = iteration_number
        attrs[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] = LangfuseObservationType.SPAN.value
        await self._save_span(
            trace_id=trace_id,
            span_id=self._new_span_id(),
            parent_span_id=root_span_id,
            name=SpanName.ITERATION_START.value,
            kind=SpanKind.INTERNAL.value,
            start_time=now,
            end_time=now,
            attributes=attrs,
            status=SpanStatus(code=SpanStatusCode.OK),
            ctx=ctx,
        )

    async def after_iteration(self, ctx: AgentContext) -> None:
        if ctx.runtime is None:
            return
        state = ctx.runtime.state
        if not isinstance(state, ReActTurnState):
            return
        trace_value = state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        root_span_id = str(root_value)
        # No decrement: AFTER_ITERATION fires at current-iteration-end, so
        # state.iteration is still the iteration that just finished.
        iteration_number = state.iteration
        now = time.time()
        start_time = self._session.iteration_start_times.pop(trace_id, None)

        attrs = self._build_base_attrs(ctx, "")
        attrs.pop(GenAiAttr.OPERATION_NAME, None)
        attrs[GenAiAttr.ITERATION_NUMBER] = iteration_number
        attrs[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] = LangfuseObservationType.SPAN.value
        if start_time is not None:
            duration_ms = int((now - start_time) * 1000)
            attrs[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT] = json.dumps(
                {"iteration": iteration_number, "duration_ms": duration_ms},
                ensure_ascii=False,
                default=str,
            )
        await self._save_span(
            trace_id=trace_id,
            span_id=self._new_span_id(),
            parent_span_id=root_span_id,
            name=SpanName.ITERATION_END.value,
            kind=SpanKind.INTERNAL.value,
            start_time=start_time if start_time is not None else now,
            end_time=now,
            attributes=attrs,
            status=SpanStatus(code=SpanStatusCode.OK),
            ctx=ctx,
        )
