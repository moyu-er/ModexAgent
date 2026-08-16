from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from bot.eval.cli import app
from bot.eval.metrics import aggregate
from typer.testing import CliRunner

from modex_agent.core.constants import StopReason
from modex_agent.memory.cleanup_hooks import CleanupMetricRecord
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel


def _write_cleanup(
    workspace: Path,
    records: list[CleanupMetricRecord],
    *,
    malformed: bool = False,
) -> None:
    metrics_dir = workspace / ".modex" / "metrics"
    metrics_dir.mkdir(parents=True)
    lines = [record.model_dump_json() for record in records]
    if malformed:
        lines.append("not-json")
    (metrics_dir / "cleanup.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_spans(workspace: Path, spans: list[SpanModel], session_id: str = "session-1") -> None:
    trace_dir = workspace / ".modex" / "runtime_state" / "coder" / "trace" / session_id
    trace_dir.mkdir(parents=True)
    payload = "\n".join(span.model_dump_json() for span in spans)
    (trace_dir / "spans.jsonl").write_text(f"{payload}\n", encoding="utf-8")


def test_aggregate_reports_cleanup_metrics_and_malformed_lines(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    _write_cleanup(
        tmp_path,
        [
            CleanupMetricRecord(
                ts=now,
                session_id="session-1",
                reason="token_budget",
                messages_kept=5,
                messages_pruned=5,
                tokens_before=100,
                tokens_after=95,
                tokens_saved=5,
                compact_generated=True,
                prune_ratio=0.5,
            ),
            CleanupMetricRecord(
                ts=now,
                session_id="session-1",
                reason="token_budget",
                messages_kept=6,
                messages_pruned=2,
                tokens_before=100,
                tokens_after=95,
                tokens_saved=5,
                compact_generated=False,
                prune_ratio=0.25,
            ),
            CleanupMetricRecord(
                ts=now,
                session_id="session-3",
                reason="manual",
                messages_kept=1,
                messages_pruned=3,
                tokens_before=100,
                tokens_after=50,
                tokens_saved=50,
                compact_generated=True,
                prune_ratio=0.75,
            ),
        ],
        malformed=True,
    )

    report = aggregate(tmp_path, days=7)

    assert "## Cleanup metrics" in report
    assert "- Triggers: 3" in report
    assert "- Malformed records: 1" in report
    assert "- Average prune ratio: 50.0%" in report
    assert "- Compact generation rate: 66.7%" in report
    assert "- Average char-estimated tokens saved: 20.0" in report
    assert "- Char-estimated token savings rate: 20.0%" in report
    assert "- Cleanup thrash events: 1" in report
    assert "- manual: 1" in report
    assert "- token_budget: 2" in report


def test_aggregate_reports_span_histograms_handoffs_and_l2_averages(tmp_path: Path) -> None:
    now = datetime.now(UTC).timestamp()
    root_span_id = "root-span"
    trace_id = "trace-1"
    _write_spans(
        tmp_path,
        [
            SpanModel(
                trace_id=trace_id,
                span_id=root_span_id,
                name=SpanName.INVOKE_AGENT,
                start_time=now,
                attributes={"stop_reason": StopReason.COMPLETED.value},
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="approval",
                parent_span_id=root_span_id,
                name=SpanName.HUMAN_REVIEW,
                start_time=now,
                attributes={GenAiAttr.APPROVAL_DECISION: "approved"},
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="handoff-1",
                parent_span_id=root_span_id,
                name=SpanName.AGENT_HANDOFF,
                start_time=now,
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="handoff-2",
                parent_span_id=root_span_id,
                name=SpanName.AGENT_HANDOFF,
                start_time=now,
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="child-root",
                parent_span_id="handoff-2",
                name=SpanName.INVOKE_AGENT,
                start_time=now,
                attributes={"stop_reason": StopReason.COMPLETED.value},
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="chat-1",
                parent_span_id=root_span_id,
                name=SpanName.CHAT,
                start_time=now,
                attributes={
                    GenAiAttr.USAGE_INPUT_TOKENS: 20,
                    GenAiAttr.USAGE_OUTPUT_TOKENS: 30,
                    GenAiAttr.USAGE_REASONING_TOKENS: 10,
                    GenAiAttr.OUTPUT_MESSAGES: [
                        {"role": "assistant", "parts": [{"type": "text", "content": "draft"}]}
                    ],
                },
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="chat-2",
                parent_span_id=root_span_id,
                name=SpanName.CHAT,
                start_time=now,
                attributes={
                    GenAiAttr.USAGE_INPUT_TOKENS: 10,
                    GenAiAttr.USAGE_OUTPUT_TOKENS: 40,
                    GenAiAttr.USAGE_REASONING_TOKENS: 20,
                    GenAiAttr.OUTPUT_MESSAGES: [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": "final answer"}],
                        }
                    ],
                },
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="tool-1",
                parent_span_id=root_span_id,
                name=SpanName.EXECUTE_TOOL,
                start_time=now,
            ),
            SpanModel(
                trace_id=trace_id,
                span_id="failed-root",
                name=SpanName.INVOKE_AGENT,
                start_time=now,
                attributes={"stop_reason": StopReason.LOOP_DETECTED.value},
            ),
        ],
    )

    report = aggregate(tmp_path, days=7)

    assert "## Stop reasons" in report
    assert f"- {StopReason.LOOP_DETECTED.value}: 1" in report
    assert "## Approval decisions" in report
    assert "- approved: 1" in report
    assert "## Handoffs\n- Count: 2" in report
    assert "## L2 score averages" in report
    assert "- Root traces: 2" in report
    assert "- No-tool traces: 1" in report
    assert "- Tool success rate: 100.0%" in report
    assert "- Reasoning depth: 15.0 tokens" in report
    assert "- Trajectory compactness: 6.0%" in report
    assert "- Overall: 51.6%" in report


def test_aggregate_excludes_records_older_than_days_window(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _write_cleanup(
        tmp_path,
        [
            CleanupMetricRecord(
                ts=now.isoformat(),
                session_id="recent",
                reason="recent_reason",
                messages_kept=1,
                messages_pruned=1,
                compact_generated=False,
                prune_ratio=0.5,
            ),
            CleanupMetricRecord(
                ts=(now - timedelta(days=2)).isoformat(),
                session_id="old",
                reason="old_reason",
                messages_kept=1,
                messages_pruned=3,
                compact_generated=True,
                prune_ratio=0.75,
            ),
        ],
    )
    _write_spans(
        tmp_path,
        [
            SpanModel(
                trace_id="recent-trace",
                span_id="recent-root",
                name=SpanName.INVOKE_AGENT,
                start_time=now.timestamp(),
                attributes={"stop_reason": StopReason.COMPLETED.value},
            ),
            SpanModel(
                trace_id="old-trace",
                span_id="old-root",
                name=SpanName.INVOKE_AGENT,
                start_time=(now - timedelta(days=2)).timestamp(),
                attributes={"stop_reason": StopReason.TIMEOUT.value},
            ),
        ],
    )

    report = aggregate(tmp_path, days=1)

    assert "- Triggers: 1" in report
    assert "- recent_reason: 1" in report
    assert "old_reason" not in report
    assert f"- {StopReason.COMPLETED.value}: 1" in report
    assert StopReason.TIMEOUT.value not in report


def test_metrics_command_reports_empty_workspace_without_langfuse_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for variable in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(variable, raising=False)

    result = CliRunner().invoke(
        app,
        ["metrics", "--workspace", str(tmp_path), "--days", "7"],
    )

    assert result.exit_code == 0
    assert "No local metrics data found for this period." in result.stdout
    assert "## Cleanup metrics" in result.stdout
    assert "## L2 score averages" in result.stdout
    assert "Langfuse trend comparison: v2 (rc.3 /api/public/v2/scores is 404)" in result.stdout
