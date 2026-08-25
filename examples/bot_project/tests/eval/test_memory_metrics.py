from __future__ import annotations

from json import dumps

import pytest
from bot.eval.memory_metrics import (
    ProbeDeltaRecord,
    UtilizationClass,
    reduce_memory_spans,
)
from pydantic import ValidationError

from modex_agent.trace.store import SpanModel

type AttributeValue = str | int | float | bool


def _span(
    name: str,
    span_id: str,
    attributes: dict[str, AttributeValue] | None = None,
) -> SpanModel:
    return SpanModel(
        trace_id=f"trace-{span_id}",
        span_id=span_id,
        name=name,
        start_time=1.0,
        end_time=2.0,
        attributes=attributes or {},
    )


def test_reduce_memory_spans_computes_compression_axes_from_span_config() -> None:
    # Given
    spans = [
        _span(
            "memory.cleanup.finished",
            "cleanup",
            {
                "memory.tokens_before": 8_000,
                "memory.tokens_after": 5_000,
                "memory.messages_kept": 4,
                "memory.messages_pruned": 6,
                "memory.prune_ratio": 0.6,
                "memory.max_token_ratio": 0.8,
                "memory.window_tokens": 10_000,
            },
        ),
    ]

    # When
    metrics = reduce_memory_spans(spans)

    # Then
    assert metrics.compression_no_data is False
    assert metrics.memory_compression_ratio == 1.0
    assert metrics.memory_compression_monotonic is True
    assert metrics.prefix_stable is True


def test_reduce_memory_spans_flags_violated_compression_invariants() -> None:
    # Given
    spans = [
        _span(
            "memory.cleanup.finished",
            "cleanup-1",
            {
                "memory.tokens_before": 9_000,
                "memory.tokens_after": 6_000,
                "memory.messages_kept": 5,
                "memory.messages_pruned": 5,
                "memory.prune_ratio": 0.5,
            },
        ),
        _span(
            "memory.cleanup.finished",
            "cleanup-2",
            {
                "memory.tokens_before": 10_000,
                "memory.tokens_after": 10_000,
                "memory.messages_kept": 4,
                "memory.messages_pruned": 6,
                "memory.prune_ratio": 0.6,
            },
        ),
    ]

    # When
    metrics = reduce_memory_spans(spans, max_token_ratio=0.8, window_tokens=10_000)

    # Then
    assert metrics.memory_compression_ratio == 1.25
    assert metrics.memory_compression_monotonic is False
    assert metrics.prefix_stable is False


def test_reduce_memory_spans_prices_two_model_write_usage_with_builtin_pricebook() -> None:
    # Given
    spans = [
        _span(
            "memory.cleanup.finished",
            "cleanup",
            {
                "memory.model": "openai/MiniMax-M2.7",
                "memory.calls": 2,
                "memory.input_tokens": 1_000_000,
                "memory.output_tokens": 100_000,
                "memory.cache_read_tokens": 200_000,
                "memory.cache_write_tokens": 400_000,
            },
        ),
        _span(
            "memory.consolidation.finished",
            "consolidation",
            {
                "memory.model": "openai/step-3.7-flash",
                "memory.calls": 1,
                "memory.input_tokens": 2_000_000,
                "memory.output_tokens": 1_000_000,
                "memory.cache_read_tokens": 500_000,
                "memory.cache_write_tokens": 0,
            },
        ),
    ]

    # When
    metrics = reduce_memory_spans(spans)

    # Then: 0.582 USD + 1.5625 USD.
    assert metrics.write_no_data is False
    assert metrics.memory_write_cost_usd == 2.1445


def test_reduce_memory_spans_computes_read_latency_and_weighted_retention() -> None:
    # Given
    first_sections = dumps(
        [
            {
                "source": "core",
                "retrieved_tokens": 100,
                "injected_tokens": 80,
                "pruned_tokens": 20,
                "priority": 1,
            },
            {
                "source": "archive",
                "retrieved_tokens": 300,
                "injected_tokens": 150,
                "pruned_tokens": 150,
                "priority": 2,
            },
        ]
    )
    second_sections = dumps(
        [
            {
                "source": "session",
                "retrieved_tokens": 100,
                "injected_tokens": 100,
                "pruned_tokens": 0,
                "priority": 0,
            }
        ]
    )
    spans = [
        _span(
            "memory.context.assembled",
            "context-1",
            {"memory.duration_ms": 10.0, "memory.sections": first_sections},
        ),
        _span(
            "memory.context.assembled",
            "context-2",
            {"memory.duration_ms": 20.0, "memory.sections": second_sections},
        ),
        _span("memory.context.assembled", "context-3", {"memory.duration_ms": 40.0}),
    ]

    # When
    metrics = reduce_memory_spans(spans)

    # Then
    assert metrics.read_no_data is False
    assert metrics.memory_read_latency_ms is not None
    assert metrics.memory_read_latency_ms.min_ms == 10.0
    assert metrics.memory_read_latency_ms.mean_ms == pytest.approx(70.0 / 3.0)
    assert metrics.memory_read_latency_ms.max_ms == 40.0
    assert metrics.injection_retention == 0.66


def test_reduce_memory_spans_counts_all_utilization_delta_classes() -> None:
    # Given
    records = [
        ProbeDeltaRecord(answer_id="a", label=UtilizationClass.BENEFICIAL),
        ProbeDeltaRecord(answer_id="b", label=UtilizationClass.BENEFICIAL),
        ProbeDeltaRecord(answer_id="c", label=UtilizationClass.HARMFUL),
        ProbeDeltaRecord(answer_id="d", label=UtilizationClass.IGNORED),
        ProbeDeltaRecord(answer_id="e", label=UtilizationClass.NEUTRAL),
    ]

    # When
    metrics = reduce_memory_spans([], probe_records=records)

    # Then
    assert metrics.utilization_no_data is False
    assert metrics.utilization_delta is not None
    assert metrics.utilization_delta.beneficial == 2
    assert metrics.utilization_delta.harmful == 1
    assert metrics.utilization_delta.ignored == 1
    assert metrics.utilization_delta.neutral == 1


def test_reduce_memory_spans_distinguishes_empty_data_and_ignores_non_memory_spans() -> None:
    # Given
    spans = [_span("chat", "chat", {"memory.duration_ms": 12.0})]

    # When
    first = reduce_memory_spans(spans)
    second = reduce_memory_spans(spans)

    # Then
    assert first == second
    assert first.compression_no_data is True
    assert first.memory_compression_ratio is None
    assert first.memory_compression_monotonic is None
    assert first.prefix_stable is None
    assert first.write_no_data is True
    assert first.memory_write_cost_usd == 0.0
    assert first.read_no_data is True
    assert first.memory_read_latency_ms is None
    assert first.injection_retention is None
    assert first.utilization_no_data is True
    assert first.utilization_delta is None


def test_reduce_memory_spans_treats_missing_attributes_as_no_data() -> None:
    # Given
    spans = [
        _span(
            "memory.cleanup.finished",
            "malformed-cleanup",
            {
                "memory.tokens_before": 100,
                "memory.messages_kept": 1,
                "memory.messages_pruned": 1,
                "memory.prune_ratio": 0.5,
            },
        ),
        _span(
            "memory.context.assembled",
            "malformed-context",
            {"memory.sections": "not-json"},
        ),
    ]

    # When
    metrics = reduce_memory_spans(spans, window_tokens=1_000)

    # Then
    assert metrics.compression_no_data is True
    assert metrics.memory_compression_monotonic is None
    assert metrics.read_no_data is True
    assert metrics.memory_read_latency_ms is None
    assert metrics.injection_retention is None


def test_reduce_memory_spans_prices_reported_model_with_zero_tokens_as_true_zero() -> None:
    # Given
    span = _span(
        "memory.consolidation.finished",
        "zero-usage",
        {
            "memory.model": "openai/step-3.7-flash",
            "memory.calls": 1,
            "memory.input_tokens": 0,
            "memory.output_tokens": 0,
            "memory.cache_read_tokens": 0,
            "memory.cache_write_tokens": 0,
        },
    )

    # When
    metrics = reduce_memory_spans([span])

    # Then
    assert metrics.write_no_data is False
    assert metrics.memory_write_cost_usd == 0.0


def test_new_metric_models_are_frozen_and_forbid_extra_fields() -> None:
    # Given / When / Then
    with pytest.raises(ValidationError):
        ProbeDeltaRecord.model_validate(
            {"answer_id": "a", "label": "neutral", "unexpected": True}
        )

    record = ProbeDeltaRecord(answer_id="a", label=UtilizationClass.NEUTRAL)
    with pytest.raises(ValidationError):
        record.answer_id = "changed"
