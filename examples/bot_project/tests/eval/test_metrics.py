from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from bot.eval.cli import app
from bot.eval.metrics import aggregate
from typer.testing import CliRunner

from modex_agent.core.emitter import StopReason
from modex_agent.trace.langfuse_query import (
    LangfuseClient,
    LangfuseQueryError,
    ObservationData,
    Provenance,
    SessionSummary,
)
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel


def _write_spans(workspace: Path, spans: list[SpanModel]) -> None:
    trace_dir = workspace / ".modex" / "runtime_state" / "coder" / "trace" / "session-1"
    trace_dir.mkdir(parents=True)
    payload = "\n".join(span.model_dump_json() for span in spans)
    (trace_dir / "spans.jsonl").write_text(f"{payload}\n", encoding="utf-8")


def _span(
    name: str,
    span_id: str,
    *,
    trace_id: str = "trace-memory",
    attributes: dict[str, str | int | float | bool] | None = None,
) -> SpanModel:
    return SpanModel(
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        start_time=datetime.now(UTC).timestamp(),
        attributes=attributes or {},
    )


def _observation(span: SpanModel, session_id: str = "session-memory") -> ObservationData:
    return ObservationData(
        id=span.span_id,
        trace_id=span.trace_id,
        start_time=datetime.fromtimestamp(span.start_time, UTC),
        end_time=None,
        parent_observation_id=span.parent_span_id,
        type="SPAN",
        name=span.name,
        level="DEFAULT",
        input=None,
        output=None,
        usage_details=None,
        metadata={f"attributes.{key}": value for key, value in span.attributes.items()},
        provided_model_name=None,
        session_id=session_id,
        latency=None,
        status_message=None,
    )


class _FakeLangfuseClient(LangfuseClient):
    def __init__(self, spans: list[SpanModel]) -> None:
        self._observations = [_observation(span) for span in spans]

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        page: int | None = None,
    ) -> list[SessionSummary]:
        del limit
        return [SessionSummary(id="session-memory", items_count=len(self._observations))] if page in (None, 1) else []

    async def get_observations(
        self,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        from_start_time: datetime | None = None,
        to_start_time: datetime | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> tuple[list[ObservationData], str | None]:
        del trace_id, to_start_time, cursor, limit
        observations = [
            item
            for item in self._observations
            if item.session_id == session_id
            and (from_start_time is None or item.start_time >= from_start_time)
        ]
        return observations, None

    async def close(self) -> None:
        return None


class _FailingLangfuseClient(_FakeLangfuseClient):
    async def list_sessions(
        self,
        *,
        limit: int = 50,
        page: int | None = None,
    ) -> list[SessionSummary]:
        del limit, page
        raise LangfuseQueryError(503, "collector unavailable")


class _RecordingInjector(L2ScoreInjector):
    def __init__(self) -> None:
        self.scores_by_trace: dict[str, list[ScoreSpec]] = {}

    async def inject_score_batch(
        self,
        trace_id: str,
        scores: list[ScoreSpec],
        *,
        observation_id: str | None = None,
    ) -> None:
        assert observation_id is None
        self.scores_by_trace[trace_id] = scores

    async def aclose(self) -> None:
        return None


def test_aggregate_reads_langfuse_memory_spans_and_injects_trace_scores(
    tmp_path: Path,
) -> None:
    spans = [
        _span(
            "memory.cleanup.finished",
            "cleanup",
            attributes={
                "memory.tokens_before": 8_000,
                "memory.tokens_after": 5_000,
                "memory.messages_kept": 4,
                "memory.messages_pruned": 6,
                "memory.prune_ratio": 0.6,
                "memory.max_token_ratio": 0.8,
                "memory.window_tokens": 10_000,
                "memory.model": "openai/step-3.7-flash",
                "memory.calls": 1,
                "memory.input_tokens": 1_000,
                "memory.output_tokens": 100,
                "memory.cache_read_tokens": 0,
                "memory.cache_write_tokens": 0,
            },
        ),
        _span(
            "memory.context.assembled",
            "context",
            attributes={
                "memory.duration_ms": 12.0,
                "memory.sections": (
                    '[{"source":"core","retrieved_tokens":100,'
                    '"injected_tokens":75,"pruned_tokens":25,"priority":100}]'
                ),
            },
        ),
    ]
    injector = _RecordingInjector()

    report = aggregate(
        tmp_path,
        days=7,
        langfuse_client=_FakeLangfuseClient(spans),
        score_injector=injector,
    )

    assert "## Memory metrics" in report
    assert "- Compression ratio: 1.0000" in report
    assert "- Mean read latency: 12.00ms" in report
    assert "- Injection retention: 75.0%" in report
    visible_scores = injector.scores_by_trace["trace-memory"]
    assert {score.name for score in visible_scores} == {
        "memory_compression_ratio",
        "memory_write_cost_usd",
        "memory_read_latency_ms",
        "memory_injection_retention",
    }
    for score in visible_scores:
        assert score.comment is not None
        provenance = Provenance.model_validate_json(score.comment)
        assert provenance.scorer == "verifier"
        assert provenance.report_source == "counters"
        assert provenance.run_ref == "trace-memory"


def test_aggregate_surfaces_langfuse_failure_explicitly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Langfuse metrics query failed.*503"):
        aggregate(
            tmp_path,
            days=7,
            langfuse_client=_FailingLangfuseClient([]),
        )


def test_aggregate_preserves_dormant_file_span_fallback(tmp_path: Path) -> None:
    now = datetime.now(UTC).timestamp()
    _write_spans(
        tmp_path,
        [
            SpanModel(
                trace_id="trace-file",
                span_id="root",
                name=SpanName.INVOKE_AGENT,
                start_time=now,
                attributes={"stop_reason": StopReason.COMPLETED.value},
            ),
            SpanModel(
                trace_id="trace-file",
                span_id="approval",
                parent_span_id="root",
                name=SpanName.HUMAN_REVIEW,
                start_time=now,
                attributes={GenAiAttr.APPROVAL_DECISION: "approved"},
            ),
        ],
    )

    report = aggregate(tmp_path, days=7)

    assert f"- {StopReason.COMPLETED.value}: 1" in report
    assert "- approved: 1" in report
    assert "- Root traces: 1" in report


def test_metrics_command_reports_empty_workspace_without_langfuse_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(variable, raising=False)

    result = CliRunner().invoke(
        app,
        ["metrics", "--workspace", str(tmp_path), "--days", "7"],
    )

    assert result.exit_code == 0
    assert "No metrics data found for this period." in result.stdout
    assert "## Memory metrics" in result.stdout
    assert "## L2 score averages" in result.stdout
