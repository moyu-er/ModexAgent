"""Shared mutable state for trace hooks.

Holds per-``trace_id`` mappings that are shared across all hook instances
created by a single ``build_trace_hooks()`` call.  Cleanup is centralized
in :meth:`TraceSessionState.clear_trace`.

This module also owns the scalar incremental metric accumulators
(:class:`MetricCounters`) that replace span read-back for L2 scoring: hooks
call :meth:`TraceSessionState.accumulate_span` as each span is persisted,
and :meth:`TraceSessionState.read_metrics` derives
:class:`~modex_agent.trace.scoring.TrajectoryMetrics` from the counters
alone — the store never needs to be read back.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final

from modex_agent.core.types import ToolCall
from modex_agent.trace.pricing import PerModelUsage, UsageBuckets
from modex_agent.trace.scoring import TrajectoryMetrics, _as_int
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel

logger = logging.getLogger(__name__)

_COST_USAGE_ATTRIBUTES: Final[tuple[GenAiAttr, GenAiAttr, GenAiAttr, GenAiAttr]] = (
    GenAiAttr.USAGE_INPUT_TOKENS,
    GenAiAttr.USAGE_OUTPUT_TOKENS,
    GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS,
    GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS,
)


def _usage_attr(attrs: dict[str, object], attr: GenAiAttr) -> int:
    """Read one usage attribute as int; missing or malformed → 0.

    Coercion semantics are shared with
    :func:`modex_agent.trace.scoring._as_int` (same function) so the
    accumulator and ``compute_metrics`` can never disagree on malformed
    attribute values.  Malformed (present but non-numeric, incl. ``bool``)
    values are logged at debug; missing attributes are skipped silently.
    """
    value = attrs.get(attr.value)
    if isinstance(value, bool) or not isinstance(value, int | float):
        if value is not None:
            logger.debug("Ignoring malformed usage attribute %s=%r", attr.value, value)
        return 0
    return _as_int(value)


class MetricCounters:
    """Scalar incremental accumulator for one ``(trace_id, root_span_id)``.

    Backing store of the write-only refactor: scalar metric counters plus
    bounded per-model usage buckets, never span objects, messages, or prompts.
    Every :class:`~modex_agent.trace.scoring.TrajectoryMetrics` field is
    derivable from these counters — the derivation in :meth:`to_metrics`
    mirrors :func:`modex_agent.trace.scoring.compute_metrics` exactly
    (locked by the parity tests in ``tests/unit/trace/test_metric_counters.py``),
    so ticket 3's ``RootSpanHook`` needs no root-span field merge on top.

    Counter → metric mapping (12 metrics ← 10 counters):

    - ``input_tokens`` → ``total_input_tokens``
    - ``output_tokens`` → ``total_output_tokens``
    - ``reasoning_tokens`` → ``total_reasoning_tokens``, ``has_reasoning``
    - ``cache_read_tokens`` → ``cache_hit_rate`` (÷ ``input_tokens``)
    - ``llm_count`` → ``llm_call_count`` (ALL chat spans)
    - ``chat_latency_sum`` + ``chat_timed_count`` → ``api_latency_avg_s``
      (denominator = chat spans with ``end_time`` set only, mirroring
      ``compute_metrics``)
    - ``tool_count`` + ``error_tool_count`` → ``tool_call_count``,
      ``error_tool_count``, ``tool_success_rate``
    - ``iteration_count`` → ``iteration_count``
    - ``input_tokens`` + ``output_tokens`` → ``response_token_ratio``

    Per-span-kind dispatch (F2/A2 binding spec):

    - ``chat`` — usage input/output/reasoning/cache_read tokens +=,
      latency sum += (``end_time - start_time``), ``llm_count`` += 1
    - ``execute_tool`` — ``tool_count`` += 1, ``error_tool_count`` += 1
      if status code is ERROR
    - ``iteration.start`` — ``iteration_count`` += 1
    - everything else (``invoke_agent`` root, handoff, approval,
      ``agent.start``, ``iteration.end``, …) — **no-op**: the root span
      carries CUMULATIVE turn usage that must never be accumulated
      (double-count hazard documented in ``scoring.py``'s chat-only filter).
    """

    __slots__ = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "per_model_usage",
        "llm_count",
        "chat_latency_sum",
        "chat_timed_count",
        "tool_count",
        "error_tool_count",
        "iteration_count",
    )

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.cache_read_tokens = 0
        self.per_model_usage: dict[str, UsageBuckets] = {}
        self.llm_count = 0
        self.chat_latency_sum = 0.0
        self.chat_timed_count = 0
        self.tool_count = 0
        self.error_tool_count = 0
        self.iteration_count = 0

    def accumulate(self, span: SpanModel) -> None:
        """Fold one span into the counters. Never raises."""
        match span.name:
            case SpanName.CHAT:
                attrs = span.attributes
                self.input_tokens += _usage_attr(attrs, GenAiAttr.USAGE_INPUT_TOKENS)
                self.output_tokens += _usage_attr(attrs, GenAiAttr.USAGE_OUTPUT_TOKENS)
                self.reasoning_tokens += _usage_attr(attrs, GenAiAttr.USAGE_REASONING_TOKENS)
                self.cache_read_tokens += _usage_attr(
                    attrs, GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS
                )
                # Cost usage comes directly from chat-span attributes; flat
                # TrajectoryMetrics lacks model splits and cache-write usage.
                model_value = attrs.get(GenAiAttr.RESPONSE_MODEL.value)
                raw_bucket_values = tuple(
                    attrs.get(attribute.value) for attribute in _COST_USAGE_ATTRIBUTES
                )
                has_valid_usage = any(
                    not isinstance(value, bool)
                    and isinstance(value, int | float)
                    and value >= 0
                    for value in raw_bucket_values
                )
                if isinstance(model_value, str) and model_value and has_valid_usage:
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens = (
                        max(0, _as_int(value)) for value in raw_bucket_values
                    )
                    previous = self.per_model_usage.get(model_value)
                    if previous is None:
                        previous = UsageBuckets()
                    self.per_model_usage[model_value] = UsageBuckets(
                        input_tokens=previous.input_tokens + input_tokens,
                        output_tokens=previous.output_tokens + output_tokens,
                        cache_read_tokens=previous.cache_read_tokens + cache_read_tokens,
                        cache_write_tokens=previous.cache_write_tokens + cache_write_tokens,
                    )
                self.llm_count += 1
                if span.end_time is not None:
                    self.chat_latency_sum += span.end_time - span.start_time
                    self.chat_timed_count += 1
            case SpanName.EXECUTE_TOOL:
                self.tool_count += 1
                if span.status.code == SpanStatusCode.ERROR:
                    self.error_tool_count += 1
            case SpanName.ITERATION_START:
                self.iteration_count += 1
            case _:
                pass  # no-op kinds — F2 double-count guard (see class docstring)

    def to_metrics(self) -> TrajectoryMetrics:
        """Derive ``TrajectoryMetrics``; zero-division guards mirror compute_metrics."""
        tool_success_rate = (
            (self.tool_count - self.error_tool_count) / self.tool_count
            if self.tool_count > 0
            else 1.0
        )
        api_latency_avg_s = (
            self.chat_latency_sum / self.chat_timed_count if self.chat_timed_count > 0 else 0.0
        )
        cache_hit_rate = self.cache_read_tokens / self.input_tokens if self.input_tokens > 0 else 0.0
        total_tokens = self.input_tokens + self.output_tokens
        response_token_ratio = self.output_tokens / total_tokens if total_tokens > 0 else 0.0
        return TrajectoryMetrics(
            tool_success_rate=tool_success_rate,
            tool_call_count=self.tool_count,
            error_tool_count=self.error_tool_count,
            iteration_count=self.iteration_count,
            llm_call_count=self.llm_count,
            total_input_tokens=self.input_tokens,
            total_output_tokens=self.output_tokens,
            total_reasoning_tokens=self.reasoning_tokens,
            api_latency_avg_s=api_latency_avg_s,
            cache_hit_rate=cache_hit_rate,
            response_token_ratio=response_token_ratio,
            has_reasoning=self.reasoning_tokens > 0,
            per_model_usage=PerModelUsage(by_model=dict(self.per_model_usage)),
        )


class TraceSessionState:
    """Mutable runtime state shared across trace hook instances.

    One instance per ``build_trace_hooks()`` call; injected into every hook
    via constructor.  All mappings are keyed by ``trace_id``.
    """

    def __init__(self) -> None:
        self._root_span_info: dict[str, tuple[str, float]] = {}
        self.llm_start_times: dict[str, float] = {}
        self.llm_request_attrs: dict[str, dict[str, object]] = {}
        self.iteration_start_times: dict[str, float] = {}
        self.tool_batch_info: dict[str, tuple[float, str, Sequence[ToolCall]]] = {}
        self.turn_usage: dict[str, dict[str, int]] = {}
        self.user_inputs: dict[str, str | None] = {}
        self._metric_counters: dict[str, dict[str, MetricCounters]] = {}

    @property
    def root_span_info(self) -> dict[str, tuple[str, float]]:
        """``trace_id`` to ``(span_id, start_time_unix_ns)`` mapping."""
        return self._root_span_info

    def accumulate_span(self, trace_id: str, root_span_id: str, span: SpanModel) -> None:
        """Fold one persisted span into the scalar counters of its root.

        Dispatches per span kind (see :class:`MetricCounters`); never
        raises — malformed usage attributes coerce to 0.  *root_span_id* is
        the ``invoke_agent`` root the span belongs to (the caller knows it
        from ``root_span_info`` / parent chain).
        """
        roots = self._metric_counters.setdefault(trace_id, {})
        counters = roots.get(root_span_id)
        if counters is None:
            counters = MetricCounters()
            roots[root_span_id] = counters
        counters.accumulate(span)

    def read_metrics(self, trace_id: str, root_span_id: str) -> TrajectoryMetrics:
        """Derive the trajectory metrics accumulated for one root span.

        By design returns the zero shape (== ``compute_metrics([])``) when
        the bucket does not exist or was cleared — not ``KeyError`` — so a
        turn with no accumulating spans scores identically on both paths.
        """
        roots = self._metric_counters.get(trace_id)
        counters = roots.get(root_span_id) if roots is not None else None
        if counters is None:
            return MetricCounters().to_metrics()
        return counters.to_metrics()

    def clear_trace(self, trace_id: str) -> None:
        """Remove all state for *trace_id* from every mapping."""
        self._root_span_info.pop(trace_id, None)
        self.llm_start_times.pop(trace_id, None)
        self.llm_request_attrs.pop(trace_id, None)
        self.iteration_start_times.pop(trace_id, None)
        self.tool_batch_info.pop(trace_id, None)
        self.turn_usage.pop(trace_id, None)
        self.user_inputs.pop(trace_id, None)
        self._metric_counters.pop(trace_id, None)
