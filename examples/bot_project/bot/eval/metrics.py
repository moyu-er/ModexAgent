"""Capability metrics aggregated from Langfuse or dormant local trace data."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import anyio
import httpx
from pydantic import BaseModel, ConfigDict

from bot.eval.memory_metric_reporting import (
    publish_memory_scores,
    render_memory_metrics,
)
from bot.eval.memory_metrics import reduce_memory_spans
from modex_agent.core.constants import StopReason
from modex_agent.trace.langfuse_query import (
    LangfuseClient,
    LangfuseQueryError,
    LangfuseTraceQuery,
)
from modex_agent.trace.score_injector import L2ScoreInjector
from modex_agent.trace.scoring import (
    TrajectoryMetrics,
    compute_metrics,
    compute_root_subtrees,
)
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import JsonlSpanQuery, SpanModel

STOP_REASON_ATTRIBUTE: Final = "stop_reason"
_SPAN_GLOB: Final = ".modex/runtime_state/*/trace/*/spans.jsonl"
_SESSION_PAGE_SIZE: Final = 50


class MetricsQueryError(RuntimeError):
    """Failure to read the Langfuse data required by the metrics report."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Langfuse metrics query failed: {detail}")


class _L2Averages(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_count: int
    no_tool_trace_count: int
    tool_success_rate: float
    tool_call_count: float
    error_tool_count: float
    iteration_count: float
    llm_call_count: float
    total_input_tokens: float
    total_output_tokens: float
    total_reasoning_tokens: float
    api_latency_avg_s: float
    cache_hit_rate: float
    response_token_ratio: float
    has_reasoning_rate: float


async def _load_file_spans(
    paths: tuple[Path, ...],
    cutoff_epoch: float,
) -> tuple[SpanModel, ...]:
    spans: list[SpanModel] = []
    for path in paths:
        query = JsonlSpanQuery(path.parent.parent)
        session_spans = await query.list_by_session(path.parent.name)
        spans.extend(span for span in session_spans if span.start_time >= cutoff_epoch)
    return tuple(spans)


async def _load_langfuse_spans(
    client: LangfuseClient,
    cutoff: datetime,
    now: datetime,
) -> tuple[SpanModel, ...]:
    query = LangfuseTraceQuery(client)
    spans: list[SpanModel] = []
    page = 1
    while True:
        sessions = await client.list_sessions(limit=_SESSION_PAGE_SIZE, page=page)
        for session in sessions:
            spans.extend(
                await query.list_by_session(
                    session.id,
                    from_start_time=cutoff,
                    to_start_time=now,
                )
            )
        if len(sessions) < _SESSION_PAGE_SIZE:
            break
        page += 1
    return tuple(spans)


def _histogram_lines(counts: Counter[str]) -> list[str]:
    if not counts:
        return ["- No data."]
    return [f"- {name}: {count}" for name, count in sorted(counts.items())]


def _l2_averages(spans: tuple[SpanModel, ...]) -> _L2Averages | None:
    root_spans = {
        span.span_id: span
        for span in spans
        if span.name == SpanName.INVOKE_AGENT.value
    }
    completed_subtrees = [
        subtree
        for root_span_id, subtree in compute_root_subtrees(list(spans)).items()
        if root_spans[root_span_id].attributes.get(STOP_REASON_ATTRIBUTE)
        == StopReason.COMPLETED.value
    ]
    if not completed_subtrees:
        return None

    metrics_list: list[TrajectoryMetrics] = [
        compute_metrics(subtree) for subtree in completed_subtrees
    ]
    trace_count = len(metrics_list)
    return _L2Averages(
        trace_count=trace_count,
        no_tool_trace_count=sum(
            not any(span.name == SpanName.EXECUTE_TOOL.value for span in subtree)
            for subtree in completed_subtrees
        ),
        tool_success_rate=sum(metric.tool_success_rate for metric in metrics_list)
        / trace_count,
        tool_call_count=sum(metric.tool_call_count for metric in metrics_list)
        / trace_count,
        error_tool_count=sum(metric.error_tool_count for metric in metrics_list)
        / trace_count,
        iteration_count=sum(metric.iteration_count for metric in metrics_list)
        / trace_count,
        llm_call_count=sum(metric.llm_call_count for metric in metrics_list)
        / trace_count,
        total_input_tokens=sum(metric.total_input_tokens for metric in metrics_list)
        / trace_count,
        total_output_tokens=sum(metric.total_output_tokens for metric in metrics_list)
        / trace_count,
        total_reasoning_tokens=sum(
            metric.total_reasoning_tokens for metric in metrics_list
        )
        / trace_count,
        api_latency_avg_s=sum(metric.api_latency_avg_s for metric in metrics_list)
        / trace_count,
        cache_hit_rate=sum(metric.cache_hit_rate for metric in metrics_list)
        / trace_count,
        response_token_ratio=sum(
            metric.response_token_ratio for metric in metrics_list
        )
        / trace_count,
        has_reasoning_rate=sum(metric.has_reasoning for metric in metrics_list)
        / trace_count,
    )


def aggregate(
    workspace_root: Path,
    days: int,
    *,
    langfuse_client: LangfuseClient | None = None,
    score_injector: L2ScoreInjector | None = None,
) -> str:
    """Return a markdown capability report and publish derived memory scores."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    async def collect() -> tuple[SpanModel, ...]:
        try:
            if langfuse_client is not None:
                spans = await _load_langfuse_spans(langfuse_client, cutoff, now)
            else:
                span_paths = tuple(sorted(workspace_root.glob(_SPAN_GLOB)))
                spans = await _load_file_spans(span_paths, cutoff.timestamp())
            if score_injector is not None:
                await publish_memory_scores(spans, score_injector)
            return spans
        except (LangfuseQueryError, httpx.HTTPError) as exc:
            raise MetricsQueryError(str(exc)) from exc
        finally:
            if score_injector is not None:
                await score_injector.aclose()
            if langfuse_client is not None:
                await langfuse_client.close()

    spans = anyio.run(collect)
    stop_reasons = Counter(
        reason
        for span in spans
        if span.name == SpanName.INVOKE_AGENT.value
        and isinstance((reason := span.attributes.get(STOP_REASON_ATTRIBUTE)), str)
    )
    approval_decisions = Counter(
        decision
        for span in spans
        if span.name == SpanName.HUMAN_REVIEW.value
        and isinstance(
            (decision := span.attributes.get(GenAiAttr.APPROVAL_DECISION.value)),
            str,
        )
    )
    handoff_count = sum(span.name == SpanName.AGENT_HANDOFF.value for span in spans)
    memory_metrics = reduce_memory_spans(list(spans))
    l2_averages = _l2_averages(spans)

    lines = [
        "# Capability metrics",
        "",
        f"- Workspace: {workspace_root.resolve()}",
        f"- Window: last {days} days",
    ]
    if not spans:
        lines.extend(["", "No metrics data found for this period."])
    lines.extend(["", *render_memory_metrics(memory_metrics)])
    lines.extend(["", "## Stop reasons", *_histogram_lines(stop_reasons)])
    lines.extend(["", "## Approval decisions", *_histogram_lines(approval_decisions)])
    lines.extend(["", "## Handoffs", f"- Count: {handoff_count}"])
    lines.extend(["", "## L2 score averages"])
    if l2_averages is None:
        lines.extend(["- Root traces: 0", "- No-tool traces: 0", "- No data."])
    else:
        lines.extend(
            [
                f"- Root traces: {l2_averages.trace_count}",
                f"- No-tool traces: {l2_averages.no_tool_trace_count}",
                f"- Tool success rate: {l2_averages.tool_success_rate:.1%}",
                f"- Avg tool calls: {l2_averages.tool_call_count:.1f}",
                f"- Avg error tools: {l2_averages.error_tool_count:.1f}",
                f"- Avg iterations: {l2_averages.iteration_count:.1f}",
                f"- Avg LLM calls: {l2_averages.llm_call_count:.1f}",
                f"- Avg input tokens: {l2_averages.total_input_tokens:.0f}",
                f"- Avg output tokens: {l2_averages.total_output_tokens:.0f}",
                f"- Avg reasoning tokens: {l2_averages.total_reasoning_tokens:.0f}",
                f"- Avg API latency: {l2_averages.api_latency_avg_s:.2f}s",
                f"- Cache hit rate: {l2_averages.cache_hit_rate:.1%}",
                f"- Response token ratio: {l2_averages.response_token_ratio:.1%}",
                f"- Reasoning trace rate: {l2_averages.has_reasoning_rate:.1%}",
            ]
        )
    return "\n".join(lines)
