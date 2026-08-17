"""Offline capability metrics aggregated from a workspace's local data."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import anyio
from pydantic import BaseModel, ConfigDict, ValidationError

from modex_agent.core.constants import StopReason
from modex_agent.memory.cleanup_hooks import CleanupMetricRecord
from modex_agent.trace.scoring import (
    TrajectoryScore,
    compute_root_subtrees,
    compute_score,
    overall_score,
)
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import JsonlSpanQuery, SpanModel

STOP_REASON_ATTRIBUTE: Final = "stop_reason"
_CLEANUP_RELATIVE_PATH: Final = Path(".modex/metrics/cleanup.jsonl")
_SPAN_GLOB: Final = ".modex/runtime_state/*/trace/*/spans.jsonl"
_LANGFUSE_NOTE: Final = "Langfuse trend comparison: v2 (rc.3 /api/public/v2/scores is 404)"


class _CleanupData(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[CleanupMetricRecord, ...]
    malformed_records: int


class _L2Averages(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_count: int
    no_tool_trace_count: int
    tool_success_rate: float
    reasoning_depth: float
    trajectory_compactness: float
    overall: float


def _record_timestamp(record: CleanupMetricRecord) -> datetime:
    timestamp = datetime.fromisoformat(record.ts)
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp


def _load_cleanup(path: Path, cutoff: datetime) -> _CleanupData:
    if not path.exists():
        return _CleanupData(records=(), malformed_records=0)

    records: list[CleanupMetricRecord] = []
    malformed_records = 0
    with path.open(encoding="utf-8") as cleanup_file:
        for line in cleanup_file:
            if not line.strip():
                continue
            try:
                record = CleanupMetricRecord.model_validate_json(line)
                timestamp = _record_timestamp(record)
            except (ValidationError, ValueError):
                malformed_records += 1
                continue
            if timestamp >= cutoff:
                records.append(record)
    return _CleanupData(records=tuple(records), malformed_records=malformed_records)


async def _load_spans(paths: tuple[Path, ...], cutoff_epoch: float) -> tuple[SpanModel, ...]:
    spans: list[SpanModel] = []
    for path in paths:
        query = JsonlSpanQuery(path.parent.parent)
        session_spans = await query.list_by_session(path.parent.name)
        spans.extend(span for span in session_spans if span.start_time >= cutoff_epoch)
    return tuple(spans)


def _histogram_lines(counts: Counter[str]) -> list[str]:
    if not counts:
        return ["- No data."]
    return [f"- {name}: {count}" for name, count in sorted(counts.items())]


def _cleanup_thrash_events(records: tuple[CleanupMetricRecord, ...]) -> int:
    previous_low_savings: dict[str, bool] = {}
    events = 0
    for record in records:
        low_savings = (
            record.tokens_before > 0
            and record.tokens_saved < 0.1 * record.tokens_before
        )
        if low_savings and previous_low_savings.get(record.session_id, False):
            events += 1
        previous_low_savings[record.session_id] = low_savings
    return events


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

    scores: list[TrajectoryScore] = [compute_score(subtree) for subtree in completed_subtrees]
    trace_count = len(scores)
    return _L2Averages(
        trace_count=trace_count,
        no_tool_trace_count=sum(
            not any(span.name == SpanName.EXECUTE_TOOL.value for span in subtree)
            for subtree in completed_subtrees
        ),
        tool_success_rate=sum(score.tool_success_rate for score in scores) / trace_count,
        reasoning_depth=sum(score.reasoning_depth for score in scores) / trace_count,
        trajectory_compactness=sum(score.trajectory_compactness for score in scores) / trace_count,
        overall=sum(overall_score(score) for score in scores) / trace_count,
    )


def aggregate(workspace_root: Path, days: int) -> str:
    """Return a markdown capability report from local cleanup and trace data."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    cleanup = _load_cleanup(workspace_root / _CLEANUP_RELATIVE_PATH, cutoff)
    span_paths = tuple(sorted(workspace_root.glob(_SPAN_GLOB)))
    spans = anyio.run(_load_spans, span_paths, cutoff.timestamp())

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
        and isinstance((decision := span.attributes.get(GenAiAttr.APPROVAL_DECISION.value)), str)
    )
    handoff_count = sum(span.name == SpanName.AGENT_HANDOFF.value for span in spans)
    l2_averages = _l2_averages(spans)

    lines = [
        "# Local capability metrics",
        "",
        f"- Workspace: {workspace_root.resolve()}",
        f"- Window: last {days} days",
    ]
    if not cleanup.records and cleanup.malformed_records == 0 and not spans:
        lines.extend(["", "No local metrics data found for this period."])

    lines.extend(
        [
            "",
            "## Cleanup metrics",
            f"- Triggers: {len(cleanup.records)}",
            f"- Malformed records: {cleanup.malformed_records}",
        ]
    )
    if cleanup.records:
        trigger_count = len(cleanup.records)
        total_tokens_before = sum(record.tokens_before for record in cleanup.records)
        total_tokens_saved = sum(record.tokens_saved for record in cleanup.records)
        token_savings_rate = (
            total_tokens_saved / total_tokens_before if total_tokens_before > 0 else 0.0
        )
        lines.extend(
            [
                f"- Average prune ratio: {sum(record.prune_ratio for record in cleanup.records) / trigger_count:.1%}",
                "- Compact generation rate: "
                f"{sum(record.compact_generated for record in cleanup.records) / trigger_count:.1%}",
                f"- Average char-estimated tokens saved: {total_tokens_saved / trigger_count:.1f}",
                f"- Char-estimated token savings rate: {token_savings_rate:.1%}",
                f"- Cleanup thrash events: {_cleanup_thrash_events(cleanup.records)}",
                "- Reasons:",
                *[f"  {line}" for line in _histogram_lines(Counter(record.reason for record in cleanup.records))],
            ]
        )
    else:
        lines.append("- No valid cleanup records.")

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
                f"- Reasoning depth: {l2_averages.reasoning_depth:.1f} tokens",
                f"- Trajectory compactness: {l2_averages.trajectory_compactness:.1%}",
                f"- Overall: {l2_averages.overall:.1%}",
            ]
        )

    lines.extend(["", f"_{_LANGFUSE_NOTE}_"])
    return "\n".join(lines)
