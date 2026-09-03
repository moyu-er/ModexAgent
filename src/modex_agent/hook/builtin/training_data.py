"""TrainingDataHook — tag a turn's trace with ``gen_ai.training.relevant``.

Fires at ``HookPoint.FINALLY_GRAPH`` (the single point where the final
``StopReason``, the total ReAct iteration count, and the full LLM token usage
are all known) — except on an approval suspend (``result=None``), where it
emits nothing (see :meth:`TrainingDataHook.finally_graph`). Computes a single
boolean — *is this turn usable as training data?* — and persists it as a
dedicated ``training_tag`` span via :meth:`OtelSpanTraceStore.save_span` so
the OTel span export can attach the ``gen_ai.training.relevant`` attribute.

L1 rules (write-time, microsecond cost — one ``save_span`` call):

1. ``result.stop_reason`` indicates failure/cancellation → ``False``
2. ``react_state.iteration`` exceeds ``max_iterations`` → ``False``
3. Total token usage (input + output, from the turn's stashed
   :class:`~modex_agent.trace.scoring.TrajectoryMetrics`) exceeds
   ``max_tokens`` → ``False``
4. Otherwise → ``True``

The factory wires this hook only when ``training_relevant=true``; an
unregistered hook means no tagging and zero overhead.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.emitter import StopReason
from modex_agent.hook.abc import OutcomeFinallyHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.scoring import TrajectoryMetrics
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus

if TYPE_CHECKING:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult

logger = logging.getLogger(__name__)

# OTel semantic-convention attribute name for training-relevance tagging.
TRAINING_RELEVANT_ATTR = GenAiAttr.TRAINING_RELEVANT

# Span name for the dedicated training-tag span emitted by this hook.
_TRAINING_TAG_SPAN_NAME = SpanName.TRAINING_TAG.value

# StopReasons that mark a turn as failure/cancellation → never training-relevant.
# ``MAX_ITERATIONS`` is intentionally excluded: a turn that ran the agent's own
# ``max_iterations`` is caught by L1 rule 2 (the ``training_max_iterations``
# check), so a legitimately long turn under the training budget can still be
# marked relevant.
_NON_TRAINING_STOP_REASONS: frozenset[StopReason] = frozenset(
    {
        StopReason.ERROR,
        StopReason.TURN_CANCELLED,
        StopReason.CANCELLED,
        StopReason.TIMEOUT,
        StopReason.LOOP_DETECTED,
        StopReason.COMMAND_INTERCEPTED,
    }
)


class TrainingDataHook(OutcomeFinallyHook):
    """Tag a turn's trace with the ``gen_ai.training.relevant`` attribute.

    Stateless across turns: every ``on_outcome`` invocation re-reads the
    ``ReActTurnState`` from ``ctx.runtime.state`` and the trace store from
    ``ctx.runtime.services.trace_store``, so pool-mode session reuse is safe.
    The suspend leg (``result=None``) is skipped by ``OutcomeFinallyHook``.
    """

    def __init__(
        self,
        *,
        max_iterations: int = 20,
        max_tokens: int = 100_000,
    ) -> None:
        self._max_iterations = max_iterations
        self._max_tokens = max_tokens

    @property
    def name(self) -> str:
        return "training_data"

    async def on_outcome(self, ctx: AgentContext, result: AgentResult) -> None:
        """Tag the turn's trace. Emitting nothing at suspend is load-bearing:
        the training exporter accepts a trajectory on ANY
        ``training_relevant=true`` tag, so a suspend-time ``true`` would
        permanently mark a turn that later terminal-finalizes as
        failed/cancelled/over-budget.
        """
        react_state = get_react_state(ctx)
        if react_state is None:
            return
        runtime = ctx.runtime
        if runtime is None:
            return
        trace_store = runtime.services.trace_store
        if trace_store is None:
            return

        relevant = self._compute_relevant(ctx, react_state, result)

        trace_id = _trace_id(ctx)
        span = _build_training_tag_span(
            trace_id=trace_id,
            session_id=_session_id(ctx),
            agent_name=_agent_name(ctx),
            invocation_id=_invocation_id(ctx),
            relevant=relevant,
        )
        try:
            await trace_store.save_span(span)
        except Exception:
            logger.warning(
                "TrainingDataHook failed to save training_relevant=%s for trace %s",
                relevant,
                trace_id,
                exc_info=True,
            )

    def _compute_relevant(
        self,
        ctx: AgentContext,
        react_state: ReActTurnState,
        result: AgentResult,
    ) -> bool:
        # L1 rule 1: stop_reason indicates failure/cancel.
        if result.stop_reason in _NON_TRAINING_STOP_REASONS:
            return False
        # L1 rule 2: iteration count exceeds training_max_iterations.
        if react_state.iteration > self._max_iterations:
            return False
        # L1 rule 3: total token usage exceeds training_max_tokens.
        return self._sum_llm_tokens(ctx) <= self._max_tokens

    def _sum_llm_tokens(self, ctx: AgentContext) -> int:
        """Sum input+output tokens from the turn's stashed trajectory metrics.

        ``RootSpanHook.finally_graph`` runs before this hook (registration
        order, priority 0) and stashes the counters-derived metrics into the
        turn state BEFORE ``clear_trace`` pops the counters bucket, so the
        stash is the only surviving per-turn token source. A missing stash
        (no root hook ran) counts as zero.
        """
        assert ctx.runtime is not None
        stashed = ctx.runtime.state.custom.get(TurnCustomKey.TRAJECTORY_METRICS)
        if isinstance(stashed, TrajectoryMetrics):
            return stashed.total_input_tokens + stashed.total_output_tokens
        return 0


# ── module-private helpers (mirror BaseTraceHook's access patterns) ─────────────


def _trace_id(ctx: AgentContext) -> str:
    """Return existing trace_id from turn state, or generate a new one.

    Matches :meth:`modex_agent.trace.base_hook.BaseTraceHook._trace_id` so
    both hooks share the same per-turn trace identifier.
    """
    if ctx.runtime is None:
        return uuid.uuid4().hex
    tid = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
    if tid is not None:
        return str(tid)
    new_id = uuid.uuid4().hex
    ctx.runtime.state.custom[TurnCustomKey.TRACE_ID] = new_id
    return new_id


def _session_id(ctx: AgentContext) -> str:
    return str(ctx.session) if ctx.session is not None else "unknown"


def _agent_name(ctx: AgentContext) -> str:
    return ctx.session.agent_name if ctx.session is not None else "unknown"


def _invocation_id(ctx: AgentContext) -> str | None:
    if ctx.session is None:
        return None
    raw = ctx.session.metadata.get("invocation_id", "")
    return str(raw) or None


def _build_training_tag_span(
    *,
    trace_id: str,
    session_id: str,
    agent_name: str,
    invocation_id: str | None,
    relevant: bool,
) -> SpanModel:
    """Build a dedicated ``training_tag`` span carrying the relevance flag."""
    attrs: dict[str, object] = {
        GenAiAttr.AGENT_NAME: agent_name,
        GenAiAttr.CONVERSATION_ID: session_id,
        TRAINING_RELEVANT_ATTR: relevant,
    }
    if invocation_id is not None:
        attrs[GenAiAttr.INVOCATION_ID] = invocation_id
    now = time.time()
    return SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex,
        parent_span_id=None,
        name=_TRAINING_TAG_SPAN_NAME,
        kind=SpanKind.INTERNAL.value,
        start_time=now,
        end_time=now,
        attributes=attrs,
        status=SpanStatus(code=SpanStatusCode.OK),
    )
