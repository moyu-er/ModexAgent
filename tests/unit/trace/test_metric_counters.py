"""Parity gate for scalar metric counters vs ``compute_metrics``.

Ticket 1 of the trace write-only refactor: ``TraceSessionState`` accumulates
scalar counters per span (``accumulate_span``) and derives
``TrajectoryMetrics`` from them (``read_metrics``). These tests lock the
counter path to ``compute_metrics(spans)`` field-by-field so the two can
never drift — the store may stop reading spans back only while this parity
holds.
"""

from __future__ import annotations

from typing import Any

import pytest

from modex_agent.trace.scoring import TrajectoryMetrics, compute_metrics
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.session_state import MetricCounters, TraceSessionState
from modex_agent.trace.store import SpanModel, SpanStatus

TRACE_ID = "trace-1"
ROOT_ID = "root-1"


# ── Span builders ──────────────────────────────────────────────────────


def _span(
    span_id: str,
    name: SpanName,
    *,
    parent: str | None = ROOT_ID,
    start: float = 1.0,
    end: float | None = 2.0,
    attributes: dict[str, Any] | None = None,
    status: SpanStatus | None = None,
) -> SpanModel:
    return SpanModel(
        trace_id=TRACE_ID,
        span_id=span_id,
        parent_span_id=parent,
        name=name.value,
        kind=SpanKind.INTERNAL,
        start_time=start,
        end_time=end,
        attributes=attributes or {},
        status=status or SpanStatus(),
    )


def _chat(
    span_id: str,
    *,
    start: float = 1.0,
    end: float | None = 2.0,
    input_tokens: object | None = None,
    output_tokens: object | None = None,
    reasoning_tokens: object | None = None,
    cache_read_tokens: object | None = None,
) -> SpanModel:
    attrs: dict[str, Any] = {}
    if input_tokens is not None:
        attrs[GenAiAttr.USAGE_INPUT_TOKENS.value] = input_tokens
    if output_tokens is not None:
        attrs[GenAiAttr.USAGE_OUTPUT_TOKENS.value] = output_tokens
    if reasoning_tokens is not None:
        attrs[GenAiAttr.USAGE_REASONING_TOKENS.value] = reasoning_tokens
    if cache_read_tokens is not None:
        attrs[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value] = cache_read_tokens
    return _span(span_id, SpanName.CHAT, start=start, end=end, attributes=attrs)


def _tool(span_id: str, *, error: bool = False) -> SpanModel:
    status = SpanStatus(code=SpanStatusCode.ERROR) if error else SpanStatus()
    return _span(
        span_id,
        SpanName.EXECUTE_TOOL,
        attributes={GenAiAttr.TOOL_NAME.value: "web_search"},
        status=status,
    )


def _root(
    span_id: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> SpanModel:
    """invoke_agent root span; may carry CUMULATIVE turn usage (F2 trap)."""
    attrs: dict[str, Any] = {}
    if input_tokens is not None:
        attrs[GenAiAttr.USAGE_INPUT_TOKENS.value] = input_tokens
    if output_tokens is not None:
        attrs[GenAiAttr.USAGE_OUTPUT_TOKENS.value] = output_tokens
    if reasoning_tokens is not None:
        attrs[GenAiAttr.USAGE_REASONING_TOKENS.value] = reasoning_tokens
    return _span(span_id, SpanName.INVOKE_AGENT, parent=None, attributes=attrs)


def _accumulate_all(state: TraceSessionState, spans: list[SpanModel]) -> None:
    for span in spans:
        state.accumulate_span(TRACE_ID, ROOT_ID, span)


def _assert_parity(spans: list[SpanModel]) -> None:
    state = TraceSessionState()
    _accumulate_all(state, spans)
    expected = compute_metrics(spans)
    actual = state.read_metrics(TRACE_ID, ROOT_ID)
    for field in TrajectoryMetrics.model_fields:
        assert getattr(actual, field) == getattr(expected, field), (
            f"parity drift on {field}: counters={getattr(actual, field)!r} "
            f"compute_metrics={getattr(expected, field)!r}"
        )
    assert actual == expected


# ── Parity gate (the core test) ────────────────────────────────────────


def test_parity_single_iteration_no_tool() -> None:
    spans = [
        _root("r", input_tokens=120, output_tokens=80),
        _chat("c1", input_tokens=120, output_tokens=80, cache_read_tokens=40),
    ]
    _assert_parity(spans)


def test_parity_multi_iteration_with_tools_and_errors() -> None:
    spans = [
        _root("r", input_tokens=600, output_tokens=300, reasoning_tokens=90),
        _span("as", SpanName.AGENT_START),
        _span("i1s", SpanName.ITERATION_START, attributes={"gen_ai.iteration.number": 1}),
        _span("i1e", SpanName.ITERATION_END),
        _chat("c1", input_tokens=200, output_tokens=100, start=1.0, end=2.5),
        _span("batch", SpanName.EXECUTE_TOOL_BATCH),
        _tool("t1"),
        _tool("t2", error=True),
        _span("i2s", SpanName.ITERATION_START),
        _span("i2e", SpanName.ITERATION_END),
        _chat("c2", input_tokens=250, output_tokens=150, reasoning_tokens=90, start=3.0, end=5.5),
        _tool("t3"),
        _span("handoff", SpanName.AGENT_HANDOFF),
        _span("review", SpanName.HUMAN_REVIEW),
        _chat("c3", input_tokens=150, output_tokens=50, start=6.0, end=6.4),
        _tool("t4", error=True),
    ]
    _assert_parity(spans)


def test_parity_chat_with_cache_read_tokens() -> None:
    spans = [
        _root("r", input_tokens=300, output_tokens=120),
        _chat("c1", input_tokens=300, output_tokens=120, cache_read_tokens=300),
        _chat("c2", input_tokens=100, output_tokens=30),
    ]
    _assert_parity(spans)


def test_parity_root_span_only_with_cumulative_usage() -> None:
    """F2 trap: root carries cumulative usage; counters must stay at zero."""
    spans = [_root("r", input_tokens=999, output_tokens=999, reasoning_tokens=999)]
    _assert_parity(spans)


def test_parity_empty_stream() -> None:
    """read_metrics on an untouched bucket == compute_metrics([]) (zeros)."""
    state = TraceSessionState()
    assert state.read_metrics(TRACE_ID, ROOT_ID) == compute_metrics([])


def test_parity_chat_span_without_end_time() -> None:
    """end_time=None chat spans count toward llm_call_count but not latency."""
    spans = [
        _root("r"),
        _chat("c1", end=None, input_tokens=50, output_tokens=10),
        _chat("c2", start=1.0, end=3.0, input_tokens=60, output_tokens=20),
    ]
    _assert_parity(spans)


def test_parity_malformed_usage_attribute_types() -> None:
    """Non-numeric usage attrs coerce to 0 identically on both paths."""
    spans = [
        _root("r"),
        _chat("c1", input_tokens="100", output_tokens=True, reasoning_tokens=[1], end=None),
        _chat("c2", input_tokens=99.7, output_tokens=1.9, start=1.0, end=2.0),
    ]
    _assert_parity(spans)


def test_parity_all_error_tools() -> None:
    spans = [
        _root("r"),
        _chat("c1", input_tokens=10, output_tokens=5),
        _tool("t1", error=True),
        _tool("t2", error=True),
    ]
    _assert_parity(spans)


# ── F2 regression ──────────────────────────────────────────────────────


def test_root_cumulative_usage_does_not_inflate_counters() -> None:
    state = TraceSessionState()
    spans = [
        _root("r", input_tokens=300, output_tokens=100),
        _chat("c1", input_tokens=100, output_tokens=40, cache_read_tokens=20),
        _chat("c2", input_tokens=200, output_tokens=60),
    ]
    _accumulate_all(state, spans)

    metrics = state.read_metrics(TRACE_ID, ROOT_ID)
    assert metrics.total_input_tokens == 300  # chat-only sum, NOT 600
    assert metrics.total_output_tokens == 100  # NOT 200
    assert metrics.llm_call_count == 2  # root is not a chat span


# ── Dispatch table ─────────────────────────────────────────────────────


def test_chat_span_accumulates_usage_and_latency() -> None:
    state = TraceSessionState()
    _accumulate_all(
        state,
        [
            _chat("c1", start=1.0, end=2.5, input_tokens=100, output_tokens=40,
                  reasoning_tokens=7, cache_read_tokens=25),
        ],
    )
    counters = state._metric_counters[TRACE_ID][ROOT_ID]
    assert counters.input_tokens == 100
    assert counters.output_tokens == 40
    assert counters.reasoning_tokens == 7
    assert counters.cache_read_tokens == 25
    assert counters.llm_count == 1
    assert counters.chat_timed_count == 1
    assert counters.chat_latency_sum == pytest.approx(1.5)


def test_chat_span_missing_attrs_count_llm_only() -> None:
    state = TraceSessionState()
    _accumulate_all(state, [_chat("c1", end=None)])
    counters = state._metric_counters[TRACE_ID][ROOT_ID]
    assert counters.llm_count == 1
    assert counters.input_tokens == 0
    assert counters.chat_latency_sum == 0.0
    assert counters.chat_timed_count == 0
    metrics = state.read_metrics(TRACE_ID, ROOT_ID)
    assert metrics.api_latency_avg_s == 0.0


def test_tool_span_dispatch_by_status() -> None:
    state = TraceSessionState()
    _accumulate_all(state, [_tool("t1"), _tool("t2", error=True)])
    counters = state._metric_counters[TRACE_ID][ROOT_ID]
    assert counters.tool_count == 2
    assert counters.error_tool_count == 1


def test_iteration_start_increments_iteration_count() -> None:
    state = TraceSessionState()
    _accumulate_all(state, [_span("i1", SpanName.ITERATION_START), _span("i2", SpanName.ITERATION_START)])
    counters = state._metric_counters[TRACE_ID][ROOT_ID]
    assert counters.iteration_count == 2


@pytest.mark.parametrize(
    "name",
    [
        SpanName.INVOKE_AGENT,
        SpanName.EXECUTE_TOOL_BATCH,
        SpanName.HUMAN_REVIEW,
        SpanName.ITERATION_END,
        SpanName.AGENT_HANDOFF,
        SpanName.AGENT_START,
        SpanName.CONTROL_COMMAND,
        SpanName.ERROR,
    ],
)
def test_no_op_span_kinds_change_nothing(name: SpanName) -> None:
    """Every non-chat/tool/iteration.start span is a no-op — even with
    cumulative usage attrs, ERROR status, and a duration (F2 guard)."""
    state = TraceSessionState()
    span = _span(
        "s",
        name,
        parent=None if name == SpanName.INVOKE_AGENT else ROOT_ID,
        start=1.0,
        end=9.0,
        attributes={
            GenAiAttr.USAGE_INPUT_TOKENS.value: 500,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: 500,
            GenAiAttr.USAGE_REASONING_TOKENS.value: 500,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 500,
        },
        status=SpanStatus(code=SpanStatusCode.ERROR),
    )
    _accumulate_all(state, [span])
    assert state.read_metrics(TRACE_ID, ROOT_ID) == compute_metrics([])


def test_accumulate_span_never_raises_on_malformed_attrs() -> None:
    state = TraceSessionState()
    weird = _chat(
        "c1",
        input_tokens={"nested": True},
        output_tokens=None,
        reasoning_tokens="many",
        cache_read_tokens=False,
    )
    _accumulate_all(state, [weird])  # must not raise
    assert state.read_metrics(TRACE_ID, ROOT_ID).llm_call_count == 1


# ── Derivation rules ───────────────────────────────────────────────────


def test_zero_division_guards() -> None:
    state = TraceSessionState()
    metrics = state.read_metrics(TRACE_ID, ROOT_ID)
    assert metrics.tool_success_rate == 1.0  # no tools → 1.0
    assert metrics.cache_hit_rate == 0.0  # no input → 0.0
    assert metrics.response_token_ratio == 0.0
    assert metrics.api_latency_avg_s == 0.0
    assert metrics.has_reasoning is False


def test_ratio_derivations() -> None:
    state = TraceSessionState()
    _accumulate_all(
        state,
        [
            _chat("c1", start=1.0, end=3.0, input_tokens=100, output_tokens=100,
                  cache_read_tokens=50),
            _chat("c2", start=5.0, end=6.0, input_tokens=100, output_tokens=300,
                  cache_read_tokens=25),
            _tool("t1"),
            _tool("t2", error=True),
        ],
    )
    metrics = state.read_metrics(TRACE_ID, ROOT_ID)
    assert metrics.tool_success_rate == pytest.approx(0.5)
    # span input_tokens is the UNCACHED count, so the hit-rate denominator is
    # uncached + cached across chat spans: 75 / (200 + 75).
    assert metrics.cache_hit_rate == pytest.approx(75 / 275)
    assert metrics.response_token_ratio == pytest.approx(400 / 600)
    assert metrics.api_latency_avg_s == pytest.approx((2.0 + 1.0) / 2)


def test_cache_hit_rate_caps_at_one_for_full_hits() -> None:
    """Every prompt token served from cache → hit rate is 1.0, not >1.

    With the old denominator (sum(input) alone) a fully-cached round
    reported input=0 and the rate collapsed to 0.0; with uncached+cached
    the same round reports 1.0.
    """
    state = TraceSessionState()
    _accumulate_all(
        state,
        [_chat("c1", input_tokens=0, output_tokens=10, cache_read_tokens=500)],
    )
    metrics = state.read_metrics(TRACE_ID, ROOT_ID)
    assert metrics.cache_hit_rate == pytest.approx(1.0)


# ── MetricCounters unit ────────────────────────────────────────────────


def test_metric_counters_starts_at_zero() -> None:
    counters = MetricCounters()
    assert counters.to_metrics() == compute_metrics([])


# ── Lifecycle ──────────────────────────────────────────────────────────


def test_clear_trace_pops_counter_bucket() -> None:
    state = TraceSessionState()
    _accumulate_all(state, [_chat("c1", input_tokens=10, output_tokens=5)])
    assert state.read_metrics(TRACE_ID, ROOT_ID).total_input_tokens == 10

    state.clear_trace(TRACE_ID)

    # By design: read after clear returns the zero shape (== compute_metrics([])),
    # not a KeyError — matches compute_metrics([]) on an empty span stream.
    assert state.read_metrics(TRACE_ID, ROOT_ID) == compute_metrics([])
    assert TRACE_ID not in state._metric_counters


def test_clear_trace_preserves_other_traces() -> None:
    state = TraceSessionState()
    _accumulate_all(state, [_chat("c1", input_tokens=10, output_tokens=5)])
    state.accumulate_span("trace-2", ROOT_ID, _chat("c2", input_tokens=70, output_tokens=30))

    state.clear_trace(TRACE_ID)

    assert state.read_metrics("trace-2", ROOT_ID).total_input_tokens == 70


def test_roots_isolated_within_trace() -> None:
    state = TraceSessionState()
    state.accumulate_span(TRACE_ID, "root-a", _chat("c1", input_tokens=10, output_tokens=5))
    state.accumulate_span(TRACE_ID, "root-b", _chat("c2", input_tokens=90, output_tokens=50))
    state.accumulate_span(TRACE_ID, "root-b", _tool("t1"))

    metrics_a = state.read_metrics(TRACE_ID, "root-a")
    metrics_b = state.read_metrics(TRACE_ID, "root-b")
    assert metrics_a.total_input_tokens == 10
    assert metrics_a.tool_call_count == 0
    assert metrics_b.total_input_tokens == 90
    assert metrics_b.tool_call_count == 1
