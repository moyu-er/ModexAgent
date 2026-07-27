"""Unit tests for framework.trace TraceQuery ABC and JsonlSpanQuery."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from modex_agent.trace.otel_store import SpanModel
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanStatusCode
from modex_agent.trace.store import JsonlSpanQuery


def _make_span(
    trace_id: str = "trace-001",
    session_id: str = "sess-001",
    agent_name: str = "test-agent",
    span_id: str = "span-001",
    name: str = "invoke_agent",
    **overrides: object,
) -> SpanModel:
    defaults: dict[str, object] = {
        "trace_id": trace_id,
        "span_id": span_id,
        "name": name,
        "start_time": 1000.0,
        "attributes": {
            GenAiAttr.AGENT_NAME: agent_name,
            GenAiAttr.CONVERSATION_ID: session_id,
        },
    }
    defaults.update(overrides)
    return SpanModel(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def tmp_query(tmp_path: Path) -> JsonlSpanQuery:
    return JsonlSpanQuery(base_dir=tmp_path)


def _write_span(path: Path, span: SpanModel) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(span.model_dump(mode="json"), ensure_ascii=False) + "\n")


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


class TestJsonlSpanQuery:
    async def test_list_by_session_returns_spans(self, tmp_path: Path) -> None:
        query = JsonlSpanQuery(base_dir=tmp_path)
        span = _make_span(trace_id="t1", session_id="s1")
        _write_span(tmp_path / "s1" / "spans.jsonl", span)

        results = await query.list_by_session("s1")
        assert len(results) == 1
        got = results[0]
        assert got.trace_id == "t1"
        assert got.name == "invoke_agent"
        assert got.attributes[GenAiAttr.CONVERSATION_ID] == "s1"

    async def test_list_by_session_empty_when_no_file(
        self, tmp_query: JsonlSpanQuery
    ) -> None:
        results = await tmp_query.list_by_session("nonexistent-session")
        assert results == []

    async def test_list_by_trace_id(self, tmp_path: Path) -> None:
        query = JsonlSpanQuery(base_dir=tmp_path)
        s1 = _make_span(trace_id="t1", session_id="s1", span_id="sp1")
        s2 = _make_span(trace_id="t2", session_id="s1", span_id="sp2")
        _write_span(tmp_path / "s1" / "spans.jsonl", s1)
        _write_span(tmp_path / "s1" / "spans.jsonl", s2)

        results = await query.list_by_trace_id("t1")
        assert len(results) == 1
        assert results[0].trace_id == "t1"

        results_t2 = await query.list_by_trace_id("t2")
        assert len(results_t2) == 1
        assert results_t2[0].trace_id == "t2"

    async def test_list_by_trace_id_across_sessions(self, tmp_path: Path) -> None:
        query = JsonlSpanQuery(base_dir=tmp_path)
        s1 = _make_span(trace_id="shared", session_id="s1", span_id="sp1")
        s2 = _make_span(trace_id="shared", session_id="s2", span_id="sp2")
        _write_span(tmp_path / "s1" / "spans.jsonl", s1)
        _write_span(tmp_path / "s2" / "spans.jsonl", s2)

        results = await query.list_by_trace_id("shared")
        assert len(results) == 2
        session_ids = {r.attributes[GenAiAttr.CONVERSATION_ID] for r in results}
        assert session_ids == {"s1", "s2"}

    async def test_list_by_trace_id_empty_when_no_base_dir(
        self, tmp_path: Path
    ) -> None:
        query = JsonlSpanQuery(base_dir=tmp_path / "does_not_exist")
        results = await query.list_by_trace_id("any")
        assert results == []

    async def test_malformed_lines_skipped(self, tmp_path: Path) -> None:
        query = JsonlSpanQuery(base_dir=tmp_path)
        span = _make_span(trace_id="t1", session_id="s1")
        path = tmp_path / "s1" / "spans.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write a valid span, then a malformed line, then another valid span
        import json

        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(span.model_dump(mode="json"), ensure_ascii=False) + "\n")
            f.write("not valid json\n")
            f.write(json.dumps(span.model_dump(mode="json"), ensure_ascii=False) + "\n")

        results = await query.list_by_session("s1")
        assert len(results) == 2  # malformed line skipped


class TestSpanModel:
    def test_span_model_frozen(self) -> None:
        span = _make_span()
        with pytest.raises(ValidationError):
            span.trace_id = "modified"  # type: ignore[misc]

    def test_span_model_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            SpanModel(
                trace_id="t",
                span_id="s",
                name="n",
                start_time=0.0,
                unknown_field="x",  # type: ignore[call-arg]
            )

    def test_span_model_defaults(self) -> None:
        span = SpanModel(
            trace_id="t",
            span_id="s",
            name="n",
            start_time=0.0,
        )
        assert span.kind == SpanKind.INTERNAL.value
        assert span.parent_span_id is None
        assert span.end_time is None
        assert span.attributes == {}
        assert span.status.code == SpanStatusCode.OK
        assert span.status.message is None
