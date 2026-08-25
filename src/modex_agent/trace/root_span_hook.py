"""Root agent span lifecycle hook."""

from __future__ import annotations

import asyncio  # noqa: F401  # noqa: ANYIO_OK
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from pydantic import BaseModel, ConfigDict

from modex_agent.core.constants import StopReason
from modex_agent.hook.abc import (
    ClosableHook,
    FinallyGraphHook,
    StartNodeTurnHook,
    is_suspend_leg,
)
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.pricing import PriceBook, compute_turn_cost, load_pricebook
from modex_agent.trace.score_injector import INJECTOR_VERSION, L2ScoreInjector, ScoreSpec
from modex_agent.trace.scoring import TrajectoryMetrics
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
    from modex_agent.trace.session_state import TraceSessionState


class _CostProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["pricing"] = "pricing"
    version: str = INJECTOR_VERSION
    report_source: Literal["local_pricebook"] = "local_pricebook"
    run_ref: str
    unpriced: list[str]
    price_source: Literal["prices_json", "model_prices_yml"]


class RootSpanHook(BaseTraceHook, StartNodeTurnHook, FinallyGraphHook, ClosableHook):
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
        environment: str = "default",
        version: str | None = None,
        tags: list[str] | None = None,
        pricebook_yml_path: Path | None = None,
    ) -> None:
        super().__init__(
            session=session,
            store=store,
            model=model,
            provider_name=provider_name,
            request_params=request_params,
            score_injector=score_injector,
            environment=environment,
            version=version,
            tags=tags,
        )
        # Fire-and-forget injection tasks. Strong references are MANDATORY:
        # the event loop keeps only weak references to tasks, so an
        # unreferenced task can be garbage-collected mid-flight. Entries
        # self-discard via done callbacks — bounded by in-flight injections,
        # never keyed by session.
        self._pending_injections: set[asyncio.Task[None]] = set()
        self._closing = False
        self._pricebook_yml_path = pricebook_yml_path
        self._pricebook: PriceBook | None = None

    @property
    def score_injector(self) -> L2ScoreInjector | None:
        return self._score_injector

    async def aclose(self) -> None:
        """Drain scheduled score injections and close their HTTP client."""
        self._closing = True
        pending = tuple(self._pending_injections)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._score_injector is not None:
            await self._score_injector.aclose()

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
        """Emit the turn's complete root span and release its trace state.

        Suspend vs terminal: ``is_suspend_leg`` (hook/abc.py — the single
        authority for this interpretation) identifies the approval-suspend
        dispatch: emit nothing, stash nothing, clear nothing — the
        scalar counters bucket MUST survive so the resumed segment
        accumulates into the same ``(trace_id, root_span_id)`` bucket and
        the terminal invocation below derives whole-turn metrics (the turn-state
        snapshot carries neither the stash nor these hook-private counters,
        so anything cleared here is lost to the resume). A suspended turn
        that is never resumed leaks at most its one scalar bucket
        (~80 bytes) for the hook's lifetime — bounded by abandoned suspends.

        Root resolution: the ``root_span_info`` entry seeded by
        :meth:`start_node_turn` — still present at the terminal invocation
        because the suspend path above no longer clears it (same-process
        resume shares this hook's session state). When the entry is missing
        anyway (cross-process crash recovery: the resumed process builds a
        fresh ``TraceSessionState`` from nothing), the root falls back to
        the snapshot-restored ``custom[TurnCustomKey.ROOT_SPAN_ID]`` with
        ``state.created_at`` — the turn's original creation time, also
        restored — so the resumed root span covers the whole turn.
        """
        assert ctx.runtime is not None
        if is_suspend_leg(result, error):
            return
        trace_id = str(ctx.runtime.state.custom[TurnCustomKey.TRACE_ID])
        root_info = self._session.root_span_info.get(trace_id)
        if root_info is None:
            root_span_id, start_time = self._resumed_root(ctx, trace_id)
        else:
            root_span_id, start_time = root_info
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
            self._stash_turn_metrics(ctx, trace_id, root_span_id)
            self._session.clear_trace(trace_id)
            return
        metrics = self._stash_turn_metrics(ctx, trace_id, root_span_id)
        if self._score_injector is not None and not self._closing:
            session_id = ctx.session.session_id
            injection_task = asyncio.create_task(
                self._inject_scores_async(
                    self._score_injector,
                    trace_id,
                    metrics,
                    root_span_id,
                    session_id,
                )
            )
            self._pending_injections.add(injection_task)
            injection_task.add_done_callback(self._pending_injections.discard)
        self._session.clear_trace(trace_id)

    def _resumed_root(self, ctx: AgentContext, trace_id: str) -> tuple[str, float]:
        """Resolve ``(root_span_id, start_time)`` when ``root_span_info`` has no entry.

        Reached on cross-process recovery resumes: a fresh process builds a
        fresh ``TraceSessionState``, while the restored turn state still
        carries ``custom[ROOT_SPAN_ID]`` and ``created_at`` (both written
        before the interrupt and checkpoint-restored). When even
        ``ROOT_SPAN_ID`` is gone — unreachable while ``start_node_turn``
        remains its only writer — the counters bucket is popped as a leak
        backstop before the ``KeyError`` surfaces through the hook error
        policy, matching the pre-refactor failure mode.
        """
        assert ctx.runtime is not None
        root_value = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if root_value is None:
            self._session.clear_trace(trace_id)
            raise KeyError(trace_id)
        return str(root_value), ctx.runtime.state.created_at

    def _stash_turn_metrics(
        self,
        ctx: AgentContext,
        trace_id: str,
        root_span_id: str,
    ) -> TrajectoryMetrics:
        """Derive the turn's metrics and per-model usage, then stash them.

        Reads ``read_metrics(trace_id, root_span_id)`` — the same
        ``(trace_id, root_span_id)`` bucket every ``_save_span`` call of this
        turn accumulated into — and stores the model on the turn-scoped
        carrier ``ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]``
        so eval-side turn aggregation can read it without touching the trace
        store. MUST run before ``clear_trace`` pops the counters bucket;
        ``read_metrics`` returns the zero shape for a missing bucket, so a
        turn with no accumulating spans stashes zeros and empty model usage.
        """
        assert ctx.runtime is not None
        metrics = self._session.read_metrics(trace_id, root_span_id)
        ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS] = metrics
        return metrics

    def _cost_score(self, metrics: TrajectoryMetrics, session_id: str) -> ScoreSpec:
        if self._pricebook is None:
            self._pricebook = load_pricebook(yml_path=self._pricebook_yml_path)
        cost = compute_turn_cost(metrics.per_model_usage, self._pricebook)
        price_source: Literal["prices_json", "model_prices_yml"] = (
            "model_prices_yml" if self._pricebook_yml_path is not None else "prices_json"
        )
        comment = _CostProvenance(
            run_ref=session_id,
            unpriced=sorted(cost.unpriced_models),
            price_source=price_source,
        ).model_dump_json()
        return ScoreSpec(
            name="cost_usd",
            value=cost.total_usd,
            data_type="NUMERIC",
            comment=comment,
        )

    async def _inject_scores_async(
        self,
        injector: L2ScoreInjector,
        trace_id: str,
        metrics: TrajectoryMetrics,
        observation_id: str,
        session_id: str,
    ) -> None:
        """Inject L2 scores off the turn's critical path; never raises."""
        try:
            cost_score = self._cost_score(metrics, session_id)
        except Exception:
            logger.warning(
                "Root trace cost score computation failed "
                "(trace_id=%s, observation_id=%s)",
                trace_id,
                observation_id,
            )
            cost_score = None
        try:
            await injector.inject_scores(
                trace_id,
                metrics,
                observation_id=observation_id,
                session_id=session_id,
                extra_scores=[cost_score] if cost_score is not None else None,
            )
        except Exception:
            logger.warning(
                "Root trace score injection failed (trace_id=%s, observation_id=%s)",
                trace_id,
                observation_id,
            )
