from __future__ import annotations

from modex_agent.trace.pricing import (
    PerModelUsage,
    PriceBook,
    TurnCost,
    UsageBuckets,
    compute_turn_cost,
)
from modex_agent.trace.scoring import TrajectoryMetrics
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName
from modex_agent.trace.session_state import MetricCounters, TraceSessionState
from modex_agent.trace.store import SpanModel

_METRIC_FIELDS = {
    "tool_success_rate",
    "tool_call_count",
    "error_tool_count",
    "iteration_count",
    "llm_call_count",
    "total_input_tokens",
    "total_output_tokens",
    "total_reasoning_tokens",
    "api_latency_avg_s",
    "cache_hit_rate",
    "response_token_ratio",
    "has_reasoning",
}


def _span(
    span_id: str,
    name: SpanName,
    attributes: dict[str, object],
) -> SpanModel:
    return SpanModel(
        trace_id="trace",
        span_id=span_id,
        parent_span_id="root",
        name=name.value,
        kind=SpanKind.CLIENT.value,
        start_time=1.0,
        end_time=2.0,
        attributes=attributes,
    )


def _chat(
    span_id: str,
    model: str | None,
    usage: dict[GenAiAttr, object],
) -> SpanModel:
    attributes = {attribute.value: value for attribute, value in usage.items()}
    if model is not None:
        attributes[GenAiAttr.RESPONSE_MODEL.value] = model
    return _span(span_id, SpanName.CHAT, attributes)


def test_existing_twelve_metric_reduction_remains_unchanged() -> None:
    # Given: the zero-valued counter shape used by a turn with no spans.
    counters = MetricCounters()

    # When: the existing twelve metrics are reduced.
    metrics = counters.to_metrics()

    # Then: every pre-T5 field and value remains byte-for-byte stable.
    assert metrics.model_dump(include=_METRIC_FIELDS) == {
        "tool_success_rate": 1.0,
        "tool_call_count": 0,
        "error_tool_count": 0,
        "iteration_count": 0,
        "llm_call_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_reasoning_tokens": 0,
        "api_latency_avg_s": 0.0,
        "cache_hit_rate": 0.0,
        "response_token_ratio": 0.0,
        "has_reasoning": False,
    }
    assert set(TrajectoryMetrics.model_fields) >= _METRIC_FIELDS


def test_turn_reduction_aggregates_four_usage_buckets_by_chat_span_model() -> None:
    # Given: multiple chat spans across two models plus a misleading cumulative root span.
    state = TraceSessionState()
    spans = [
        _chat(
            "chat-a-1",
            "model-a",
            {
                GenAiAttr.USAGE_INPUT_TOKENS: 100,
                GenAiAttr.USAGE_OUTPUT_TOKENS: 20,
                GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS: 30,
                GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS: 4,
            },
        ),
        _chat(
            "chat-b",
            "model-b",
            {
                GenAiAttr.USAGE_INPUT_TOKENS: 70,
                GenAiAttr.USAGE_OUTPUT_TOKENS: 11,
                GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS: 5,
                GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS: 9,
            },
        ),
        _chat(
            "chat-a-2",
            "model-a",
            {
                GenAiAttr.USAGE_INPUT_TOKENS: 40,
                GenAiAttr.USAGE_OUTPUT_TOKENS: 8,
                GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS: 6,
                GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS: 2,
            },
        ),
        _span(
            "root",
            SpanName.INVOKE_AGENT,
            {
                GenAiAttr.RESPONSE_MODEL.value: "model-a",
                GenAiAttr.USAGE_INPUT_TOKENS.value: 999,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 999,
                GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 999,
                GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS.value: 999,
            },
        ),
    ]
    for span in spans:
        state.accumulate_span("trace", "root", span)

    # When: the turn is reduced from its chat-span accumulator bucket.
    reduction = state.read_metrics("trace", "root")

    # Then: usage is grouped exactly by response model and the flat metrics stay unchanged.
    assert reduction.per_model_usage == PerModelUsage(
        by_model={
            "model-a": UsageBuckets(
                input_tokens=140,
                output_tokens=28,
                cache_read_tokens=36,
                cache_write_tokens=6,
            ),
            "model-b": UsageBuckets(
                input_tokens=70,
                output_tokens=11,
                cache_read_tokens=5,
                cache_write_tokens=9,
            ),
        }
    )
    assert reduction.model_dump(include=_METRIC_FIELDS) == {
        "tool_success_rate": 1.0,
        "tool_call_count": 0,
        "error_tool_count": 0,
        "iteration_count": 0,
        "llm_call_count": 3,
        "total_input_tokens": 210,
        "total_output_tokens": 39,
        "total_reasoning_tokens": 0,
        "api_latency_avg_s": 1.0,
        "cache_hit_rate": 41 / 210,
        "response_token_ratio": 39 / 249,
        "has_reasoning": False,
    }


def test_usage_aggregation_skips_chat_spans_without_model_or_valid_usage() -> None:
    # Given: malformed or incomplete chat-span attributes at the trace boundary.
    state = TraceSessionState()
    spans = [
        _chat(
            "missing-model",
            None,
            {GenAiAttr.USAGE_INPUT_TOKENS: 30},
        ),
        _chat("missing-usage", "model-a", {}),
        _chat(
            "malformed-usage",
            "model-b",
            {
                GenAiAttr.USAGE_INPUT_TOKENS: "30",
                GenAiAttr.USAGE_OUTPUT_TOKENS: True,
            },
        ),
    ]
    for span in spans:
        state.accumulate_span("trace", "root", span)

    # When: the turn is reduced.
    reduction = state.read_metrics("trace", "root")

    # Then: no incomplete span creates a priced model entry.
    assert reduction.per_model_usage == PerModelUsage()


def test_usage_aggregation_is_empty_without_chat_spans() -> None:
    # Given: a fresh turn accumulator with no chat spans.
    state = TraceSessionState()

    # When: the empty turn is reduced.
    reduction = state.read_metrics("trace", "root")

    # Then: the additive cost source is empty.
    assert reduction.per_model_usage == PerModelUsage()


def test_empty_usage_reduction_costs_zero_without_unpriced_models() -> None:
    # Given: a turn with no chat spans and an empty local pricebook.
    state = TraceSessionState()
    pricebook = PriceBook(models={})

    # When: the reduction product is passed directly to the pricing seam.
    cost = compute_turn_cost(
        state.read_metrics("trace", "root").per_model_usage,
        pricebook,
    )

    # Then: empty usage has zero cost and no misleading unpriced models.
    assert cost == TurnCost(total_usd=0.0, by_model={}, unpriced_models=[])


def test_usage_aggregation_resets_between_sequential_turns() -> None:
    # Given: one completed turn whose trace bucket is cleared before the next turn.
    state = TraceSessionState()
    state.accumulate_span(
        "turn-1",
        "root-1",
        _chat("chat-1", "model-a", {GenAiAttr.USAGE_INPUT_TOKENS: 100}),
    )
    state.clear_trace("turn-1")
    state.accumulate_span(
        "turn-2",
        "root-2",
        _chat("chat-2", "model-b", {GenAiAttr.USAGE_OUTPUT_TOKENS: 7}),
    )

    # When: the second turn is reduced.
    reduction = state.read_metrics("turn-2", "root-2")

    # Then: no first-turn model or usage leaks into it.
    assert reduction.per_model_usage == PerModelUsage(
        by_model={"model-b": UsageBuckets(output_tokens=7)}
    )
