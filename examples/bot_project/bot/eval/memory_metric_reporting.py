"""Memory metric report rendering and Langfuse score publication."""

from __future__ import annotations

from collections import defaultdict
from typing import Final

from bot.eval.memory_metric_models import MemoryMetrics
from bot.eval.memory_metrics import reduce_memory_spans
from modex_agent.trace.langfuse_query import Provenance
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec
from modex_agent.trace.store import SpanModel

_MEMORY_SPAN_PREFIX: Final = "memory."
_MEMORY_SCORE_VERSION: Final = "memory_metrics.v1"


def _score_specs(metrics: MemoryMetrics, trace_id: str) -> list[ScoreSpec]:
    comment = Provenance(
        scorer="verifier",
        version=_MEMORY_SCORE_VERSION,
        report_source="counters",
        run_ref=trace_id,
    ).model_dump_json()
    scores: list[ScoreSpec] = []
    if metrics.memory_compression_ratio is not None:
        scores.append(
            ScoreSpec(
                name="memory_compression_ratio",
                value=metrics.memory_compression_ratio,
                data_type="NUMERIC",
                comment=comment,
            )
        )
    if not metrics.write_no_data:
        scores.append(
            ScoreSpec(
                name="memory_write_cost_usd",
                value=metrics.memory_write_cost_usd,
                data_type="NUMERIC",
                comment=comment,
            )
        )
    if metrics.memory_read_latency_ms is not None:
        scores.append(
            ScoreSpec(
                name="memory_read_latency_ms",
                value=metrics.memory_read_latency_ms.mean_ms,
                data_type="NUMERIC",
                comment=comment,
            )
        )
    if metrics.injection_retention is not None:
        scores.append(
            ScoreSpec(
                name="memory_injection_retention",
                value=metrics.injection_retention,
                data_type="NUMERIC",
                comment=comment,
            )
        )
    return scores


async def publish_memory_scores(
    spans: tuple[SpanModel, ...],
    injector: L2ScoreInjector,
) -> None:
    """Publish available run-level memory metrics once per trace."""
    spans_by_trace: dict[str, list[SpanModel]] = defaultdict(list)
    for span in spans:
        if span.name.startswith(_MEMORY_SPAN_PREFIX):
            spans_by_trace[span.trace_id].append(span)
    for trace_id, trace_spans in spans_by_trace.items():
        scores = _score_specs(reduce_memory_spans(trace_spans), trace_id)
        if scores:
            await injector.inject_score_batch(trace_id, scores)


def render_memory_metrics(metrics: MemoryMetrics) -> list[str]:
    """Render the deterministic memory metric group as Markdown lines."""
    compression = (
        "no data"
        if metrics.memory_compression_ratio is None
        else f"{metrics.memory_compression_ratio:.4f}"
    )
    latency = (
        "no data"
        if metrics.memory_read_latency_ms is None
        else f"{metrics.memory_read_latency_ms.mean_ms:.2f}ms"
    )
    retention = (
        "no data"
        if metrics.injection_retention is None
        else f"{metrics.injection_retention:.1%}"
    )
    write_cost = (
        "no data"
        if metrics.write_no_data
        else f"${metrics.memory_write_cost_usd:.6f}"
    )
    return [
        "## Memory metrics",
        f"- Compression ratio: {compression}",
        f"- Strict token reduction: {metrics.memory_compression_monotonic}",
        f"- Prefix stable: {metrics.prefix_stable}",
        f"- Write cost: {write_cost}",
        f"- Mean read latency: {latency}",
        f"- Injection retention: {retention}",
    ]
