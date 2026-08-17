"""Root agent span lifecycle hook."""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, overload

from modex_agent.core.constants import StopReason
from modex_agent.hook.abc import FinallyGraphHook, StartNodeTurnHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.scoring import compute_root_subtrees, compute_score
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanStatus

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.score_injector import L2ScoreInjector
    from modex_agent.trace.session_state import TraceSessionState


class RootSpanHook(BaseTraceHook, StartNodeTurnHook, FinallyGraphHook):
    """Emit one complete root span after an agent turn finishes."""

    def __init__(
        self,
        *,
        session: TraceSessionState,
        store: OtelSpanTraceStore | None,
        model: str | None = None,
        provider_name: str | None = None,
        request_params: dict[str, object] | None = None,
        score_injector: L2ScoreInjector | None = None,
    ) -> None:
        super().__init__(
            session=session,
            store=store,
            model=model,
            provider_name=provider_name,
            request_params=request_params,
            score_injector=score_injector,
        )

    async def start_node_turn(self, ctx: AgentContext) -> None:
        """Register root span state without emitting an incomplete span."""
        assert ctx.runtime is not None
        # Reuse inherited trace_id (subagent linking); root_span_id is always new.
        trace_id = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        if trace_id is None:
            trace_id = uuid.uuid4().hex
            ctx.runtime.state.custom[TurnCustomKey.TRACE_ID] = trace_id
        root_span_id = self._new_span_id()
        ctx.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID] = root_span_id
        self._session.root_span_info[trace_id] = (root_span_id, time.time())
        self._session.user_inputs[trace_id] = await self._last_user_input(ctx)

    @overload
    async def finally_graph(
        self,
        ctx: AgentContext,
        result: AgentResult | None,
    ) -> None: ...

    @overload
    async def finally_graph(
        self,
        ctx: AgentContext,
        *,
        result: AgentResult | None = None,
        error: Exception | None = None,
    ) -> None: ...

    async def finally_graph(
        self,
        ctx: AgentContext,
        result: AgentResult | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Emit the turn's complete root span and release its trace state."""
        assert ctx.runtime is not None
        trace_id = str(ctx.runtime.state.custom[TurnCustomKey.TRACE_ID])
        root_span_id, start_time = self._session.root_span_info[trace_id]
        parent_value = ctx.runtime.state.custom.get(TurnCustomKey.PARENT_SPAN_ID)
        parent_span_id = str(parent_value) if parent_value is not None else None

        attributes = self._build_base_attrs(ctx, SpanName.INVOKE_AGENT.value)
        attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] = LangfuseObservationType.AGENT.value
        attributes[GenAiAttr.LANGFUSE_INTERNAL_AS_ROOT] = True
        attributes[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] = self._session.user_inputs.get(trace_id)

        if parent_span_id is not None:
            attributes[GenAiAttr.LANGFUSE_SESSION_ID] = ctx.session.parent_session_id

        if result is not None:
            attributes[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT] = result.content
            attributes["stop_reason"] = str(result.stop_reason)
            attributes[GenAiAttr.RESPONSE_FINISH_REASONS] = [str(result.stop_reason).lower()]

        turn_usage = self._session.turn_usage.get(trace_id)
        if turn_usage is not None:
            usage_attributes = (
                (GenAiAttr.USAGE_INPUT_TOKENS, "input_tokens"),
                (GenAiAttr.USAGE_OUTPUT_TOKENS, "output_tokens"),
                (GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS, "cache_read_input_tokens"),
                (
                    GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS,
                    "cache_creation_input_tokens",
                ),
                (GenAiAttr.USAGE_REASONING_TOKENS, "reasoning_tokens"),
            )
            for attribute, usage_key in usage_attributes:
                usage = turn_usage.get(usage_key, 0)
                if usage > 0:
                    attributes[attribute] = usage

        failed = error is not None or result is None
        status = SpanStatus(
            code=SpanStatusCode.ERROR if failed else SpanStatusCode.OK,
            message=str(error) if error is not None else None,
        )
        await self._save_span(
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=parent_span_id,
            name=SpanName.INVOKE_AGENT.value,
            kind=SpanKind.INTERNAL.value,
            start_time=start_time,
            end_time=time.time(),
            attributes=attributes,
            status=status,
            ctx=ctx,
        )
        if result is None or result.stop_reason is not StopReason.COMPLETED:
            self._session.clear_trace(trace_id)
            return
        if self._score_injector is not None and self._store is not None:
            try:
                spans = await self._store.list_by_trace_id(trace_id)
                subtree = compute_root_subtrees(spans)[root_span_id]
                await self._score_injector.inject_scores(
                    trace_id,
                    compute_score(subtree),
                    observation_id=root_span_id,
                )
            except Exception:
                logger.warning(
                    "Root trace score injection failed (trace_id=%s, observation_id=%s)",
                    trace_id,
                    root_span_id,
                )
        self._session.clear_trace(trace_id)
