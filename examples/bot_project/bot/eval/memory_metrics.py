"""Deterministic reduction of memory lifecycle spans into eval metrics."""

from __future__ import annotations

from collections import Counter
from itertools import pairwise
from math import isfinite
from statistics import fmean
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from bot.eval.memory_metric_models import (
    DistributionStats,
    MemoryMetrics,
    ProbeDeltaRecord,
    UtilizationClass,
    UtilizationDelta,
)
from modex_agent.memory.hooks import SectionProvenance
from modex_agent.trace.pricing import (
    PerModelUsage,
    PriceBook,
    UsageBuckets,
    compute_turn_cost,
    load_pricebook,
)
from modex_agent.trace.store import SpanModel

_CLEANUP_FINISHED: Final[str] = "memory.cleanup.finished"
_CONSOLIDATION_FINISHED: Final[str] = "memory.consolidation.finished"
_CONTEXT_ASSEMBLED: Final[str] = "memory.context.assembled"
_WRITE_SPAN_NAMES: Final[frozenset[str]] = frozenset(
    {_CLEANUP_FINISHED, _CONSOLIDATION_FINISHED}
)

_TOKENS_BEFORE: Final[str] = "memory.tokens_before"
_TOKENS_AFTER: Final[str] = "memory.tokens_after"
_MESSAGES_KEPT: Final[str] = "memory.messages_kept"
_MESSAGES_PRUNED: Final[str] = "memory.messages_pruned"
_PRUNE_RATIO: Final[str] = "memory.prune_ratio"
_MAX_TOKEN_RATIO: Final[str] = "memory.max_token_ratio"
_WINDOW_TOKENS: Final[str] = "memory.window_tokens"
_MODEL: Final[str] = "memory.model"
_CALLS: Final[str] = "memory.calls"
_INPUT_TOKENS: Final[str] = "memory.input_tokens"
_OUTPUT_TOKENS: Final[str] = "memory.output_tokens"
_CACHE_READ_TOKENS: Final[str] = "memory.cache_read_tokens"
_CACHE_WRITE_TOKENS: Final[str] = "memory.cache_write_tokens"
_DURATION_MS: Final[str] = "memory.duration_ms"
_SECTIONS: Final[str] = "memory.sections"

type NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
type NonnegativeFloat = Annotated[float, Field(ge=0)]

_INT_ADAPTER: Final[TypeAdapter[NonnegativeInt]] = TypeAdapter(NonnegativeInt)
_NUMBER_ADAPTER: Final[TypeAdapter[int | float]] = TypeAdapter(
    int | float,
    config=ConfigDict(strict=True),
)
_STRING_ADAPTER: Final[TypeAdapter[str]] = TypeAdapter(str, config=ConfigDict(strict=True))


class _CleanupSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens_before: NonnegativeInt
    tokens_after: NonnegativeInt
    messages_kept: NonnegativeInt
    messages_pruned: NonnegativeInt
    prune_ratio: NonnegativeFloat
    bound_tokens: NonnegativeFloat | None


_SECTIONS_ADAPTER: Final[TypeAdapter[list[SectionProvenance]]] = TypeAdapter(
    list[SectionProvenance]
)


def _read_int(span: SpanModel, key: str) -> int | None:
    try:
        return _INT_ADAPTER.validate_python(span.attributes.get(key))
    except ValidationError:
        return None


def _read_number(span: SpanModel, key: str) -> float | None:
    try:
        value = _NUMBER_ADAPTER.validate_python(span.attributes.get(key))
    except ValidationError:
        return None
    number = float(value)
    return number if isfinite(number) and number >= 0 else None


def _read_string(span: SpanModel, key: str) -> str | None:
    try:
        value = _STRING_ADAPTER.validate_python(span.attributes.get(key))
    except ValidationError:
        return None
    return value if value else None


def _cleanup_sample(
    span: SpanModel,
    max_token_ratio: float,
    window_tokens: int | None,
) -> _CleanupSample | None:
    tokens_before = _read_int(span, _TOKENS_BEFORE)
    tokens_after = _read_int(span, _TOKENS_AFTER)
    messages_kept = _read_int(span, _MESSAGES_KEPT)
    messages_pruned = _read_int(span, _MESSAGES_PRUNED)
    prune_ratio = _read_number(span, _PRUNE_RATIO)
    if (
        tokens_before is None
        or tokens_after is None
        or messages_kept is None
        or messages_pruned is None
        or prune_ratio is None
    ):
        return None

    span_ratio = _read_number(span, _MAX_TOKEN_RATIO)
    span_window = _read_int(span, _WINDOW_TOKENS)
    effective_ratio = span_ratio if span_ratio is not None else max_token_ratio
    effective_window = span_window if span_window is not None else window_tokens
    bound = (
        effective_ratio * effective_window
        if effective_ratio > 0 and effective_window is not None and effective_window > 0
        else None
    )
    return _CleanupSample(
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        messages_kept=messages_kept,
        messages_pruned=messages_pruned,
        prune_ratio=prune_ratio,
        bound_tokens=bound,
    )


def _usage_from_span(span: SpanModel) -> tuple[str, UsageBuckets] | None:
    model = _read_string(span, _MODEL)
    calls = _read_int(span, _CALLS)
    input_tokens = _read_int(span, _INPUT_TOKENS)
    output_tokens = _read_int(span, _OUTPUT_TOKENS)
    cache_read_tokens = _read_int(span, _CACHE_READ_TOKENS)
    cache_write_tokens = _read_int(span, _CACHE_WRITE_TOKENS)
    if (
        model is None
        or calls is None
        or input_tokens is None
        or output_tokens is None
        or cache_read_tokens is None
        or cache_write_tokens is None
    ):
        return None
    return model, UsageBuckets(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def _read_sections(span: SpanModel) -> list[SectionProvenance] | None:
    raw = _read_string(span, _SECTIONS)
    if raw is None:
        return None
    try:
        return _SECTIONS_ADAPTER.validate_json(raw)
    except ValidationError:
        return None


def reduce_memory_spans(
    spans: list[SpanModel],
    probe_records: list[ProbeDeltaRecord] | None = None,
    *,
    pricebook: PriceBook | None = None,
    max_token_ratio: float = 1.0,
    window_tokens: int | None = None,
) -> MemoryMetrics:
    """Reduce memory spans and optional probe labels without remote dependencies."""
    cleanup_spans = [span for span in spans if span.name == _CLEANUP_FINISHED]
    cleanup_samples = [
        sample
        for span in cleanup_spans
        if (sample := _cleanup_sample(span, max_token_ratio, window_tokens)) is not None
    ]
    cleanup_complete = bool(cleanup_spans) and len(cleanup_samples) == len(cleanup_spans)
    monotonic = (
        all(sample.tokens_after < sample.tokens_before for sample in cleanup_samples)
        if cleanup_complete
        else None
    )
    prefix_stable = (
        all(current.messages_kept >= previous.messages_kept for previous, current in pairwise(cleanup_samples))
        if cleanup_complete
        else None
    )
    bounds_complete = cleanup_complete and all(sample.bound_tokens is not None for sample in cleanup_samples)
    compression_ratio = (
        max(sample.tokens_before / sample.bound_tokens for sample in cleanup_samples if sample.bound_tokens is not None)
        if bounds_complete
        else None
    )

    usage_by_model: dict[str, UsageBuckets] = {}
    for span in spans:
        if span.name not in _WRITE_SPAN_NAMES:
            continue
        parsed_usage = _usage_from_span(span)
        if parsed_usage is None:
            continue
        model, usage = parsed_usage
        current = usage_by_model.get(model, UsageBuckets())
        usage_by_model[model] = UsageBuckets(
            input_tokens=current.input_tokens + usage.input_tokens,
            output_tokens=current.output_tokens + usage.output_tokens,
            cache_read_tokens=current.cache_read_tokens + usage.cache_read_tokens,
            cache_write_tokens=current.cache_write_tokens + usage.cache_write_tokens,
        )
    write_cost = (
        compute_turn_cost(
            PerModelUsage(by_model=usage_by_model),
            pricebook or load_pricebook(yml_path=None),
        ).total_usd
        if usage_by_model
        else 0.0
    )

    context_spans = [span for span in spans if span.name == _CONTEXT_ASSEMBLED]
    latencies = [
        latency
        for span in context_spans
        if (latency := _read_number(span, _DURATION_MS)) is not None
    ]
    latency_stats = (
        DistributionStats(min_ms=min(latencies), mean_ms=fmean(latencies), max_ms=max(latencies))
        if latencies
        else None
    )
    sections = [
        section
        for span in context_spans
        for section in (_read_sections(span) or [])
    ]
    retrieved_tokens = sum(section.retrieved_tokens for section in sections)
    injection_retention = (
        sum(section.injected_tokens for section in sections) / retrieved_tokens
        if retrieved_tokens > 0
        else None
    )

    label_counts = Counter(record.label for record in probe_records or [])
    utilization_delta = (
        UtilizationDelta(
            beneficial=label_counts[UtilizationClass.BENEFICIAL],
            harmful=label_counts[UtilizationClass.HARMFUL],
            ignored=label_counts[UtilizationClass.IGNORED],
            neutral=label_counts[UtilizationClass.NEUTRAL],
        )
        if probe_records
        else None
    )
    return MemoryMetrics(
        compression_no_data=not bounds_complete,
        memory_compression_ratio=compression_ratio,
        memory_compression_monotonic=monotonic,
        prefix_stable=prefix_stable,
        write_no_data=not usage_by_model,
        memory_write_cost_usd=write_cost,
        read_no_data=latency_stats is None and injection_retention is None,
        memory_read_latency_ms=latency_stats,
        injection_retention=injection_retention,
        utilization_no_data=utilization_delta is None,
        utilization_delta=utilization_delta,
    )
