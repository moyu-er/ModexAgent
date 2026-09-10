from __future__ import annotations

import base64
import json
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from modex_agent.core.message import MessageRole
from modex_agent.trace import langfuse_query
from modex_agent.trace.langfuse_query import (
    LangfuseClient,
    LangfuseQueryError,
    LangfuseTraceQuery,
    ObservationData,
    observation_to_span,
)
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanModel, SpanStatus
from modex_agent.trace.training_exporter import TrainingDataExporter

TRACE_ID = "0123456789abcdef0123456789abc003"


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing MockTransport must fail locally, never contact a service."""
    def reject_sync(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Langfuse tests require MockTransport")

    async def reject_async(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Langfuse tests require MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject_async)


def _observation(
    *,
    observation_id: str = "obs-chat",
    start_time: str = "2026-08-17T10:00:01Z",
    name: str = "chat",
    observation_type: str = "GENERATION",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": observation_id,
        "traceId": TRACE_ID,
        "startTime": start_time,
        "endTime": "2026-08-17T10:00:02Z",
        "parentObservationId": "obs-root",
        "type": observation_type,
        "name": name,
        "level": "DEFAULT",
        "statusMessage": None,
        "input": "user: hello verify",
        "output": "assistant: verified",
        "usageDetails": {
            "input": 10,
            "output": 5,
            "total": 15,
            "input_cached_tokens": 128,
        },
        "metadata": metadata
        or {
            "attributes.gen_ai.request.model": "metadata-model",
            "attributes.gen_ai.usage.cache_read.input_tokens": 128,
        },
        "providedModelName": None,
        "sessionId": "session-1",
        "latency": 1.0,
    }


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    async_client = httpx.AsyncClient

    def build_client(**kwargs: Any) -> httpx.AsyncClient:
        return async_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", build_client)




async def test_get_observations_sends_projection_auth_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_auth = base64.b64encode(b"public:secret").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Basic {expected_auth}"
        assert request.url.params["fields"] == "core,basic,io,usage,metadata,model"
        assert request.url.params["sessionId"] == "session-1"
        assert request.url.params["traceId"] == TRACE_ID
        assert request.url.params["fromStartTime"] == "2026-08-17T09:00:00+00:00"
        assert request.url.params["toStartTime"] == "2026-08-17T11:00:00+00:00"
        assert request.url.params["cursor"] == "cursor-1"
        assert request.url.params["limit"] == "500"
        return httpx.Response(200, json={"data": [_observation()], "meta": {"cursor": "next"}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        observations, cursor = await client.get_observations(
            session_id="session-1",
            trace_id=TRACE_ID,
            from_start_time=datetime(2026, 8, 17, 9, tzinfo=UTC),
            to_start_time=datetime(2026, 8, 17, 11, tzinfo=UTC),
            cursor="cursor-1",
        )
    finally:
        await client.close()

    assert [observation.id for observation in observations] == ["obs-chat"]
    assert cursor == "next"


async def test_get_scores_sends_projection_auth_filters_and_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    expected_auth = base64.b64encode(b"public:secret").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/public/v3/scores"
        assert request.headers["Authorization"] == f"Basic {expected_auth}"
        assert dict(request.url.params) == {
            "fields": "core,details,subject",
            "limit": "25",
            "traceId": TRACE_ID,
            "name": "trajectory_quality",
            "fromTimestamp": "2026-08-17T09:00:00Z",
            "toTimestamp": "2026-08-17T11:00:00Z",
            "cursor": "cursor-1",
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "score-1",
                        "name": "trajectory_quality",
                        "value": 0.75,
                        "dataType": "NUMERIC",
                        "comment": '{"scorer":"trajectory"}',
                        "subject": {"kind": "TRACE", "id": TRACE_ID},
                    }
                ],
                "meta": {"page": {"nextCursor": "cursor-2"}},
            },
        )

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")

    # When
    try:
        scores, cursor = await client.get_scores(
            trace_id=TRACE_ID,
            name="trajectory_quality",
            from_timestamp="2026-08-17T09:00:00Z",
            to_timestamp="2026-08-17T11:00:00Z",
            limit=25,
            cursor="cursor-1",
        )
    finally:
        await client.close()

    # Then
    assert [(score.name, score.value, score.data_type) for score in scores] == [
        ("trajectory_quality", 0.75, "NUMERIC")
    ]
    assert scores[0].comment == '{"scorer":"trajectory"}'
    assert scores[0].subject == langfuse_query.ScoreSubject(kind="TRACE", id=TRACE_ID)
    assert cursor == "cursor-2"


async def test_get_scores_parses_live_subject_shape_with_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "closure_probe_test",
                        "value": 1.0,
                        "dataType": "NUMERIC",
                        "subject": {
                            "kind": "trace",
                            "id": TRACE_ID,
                            "traceId": TRACE_ID,
                            "serverAdded": "ignored",
                        },
                    }
                ],
                "meta": {"page": {}},
            },
        )

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")

    # When
    try:
        scores, _ = await client.get_scores(name="closure_probe_test")
    finally:
        await client.close()

    # Then
    assert scores[0].subject is not None
    assert scores[0].subject.kind == "trace"
    assert scores[0].subject.id == TRACE_ID
    assert scores[0].subject.model_dump() == {"kind": "trace", "id": TRACE_ID}


async def test_get_scores_preserves_boolean_value_and_unknown_data_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "verified",
                        "value": True,
                        "dataType": "CUSTOM_BOOLEAN",
                    }
                ],
                "meta": {"page": {}},
            },
        )

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")

    # When
    try:
        scores, cursor = await client.get_scores()
    finally:
        await client.close()

    # Then
    assert scores == [
        langfuse_query.ScoreReadData(
            name="verified",
            value=True,
            data_type="CUSTOM_BOOLEAN",
            comment=None,
            subject=None,
        )
    ]
    assert cursor is None


@pytest.mark.parametrize("limit", [0, -1])
async def test_get_scores_defaults_empty_fields_and_forwards_limit(
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
) -> None:
    # Given
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"] == "core,details,subject"
        assert request.url.params["limit"] == str(limit)
        return httpx.Response(200, json={"data": [], "meta": {"page": {}}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")

    # When
    try:
        scores, cursor = await client.get_scores(fields="", limit=limit)
    finally:
        await client.close()

    # Then
    assert scores == []
    assert cursor is None


async def test_get_scores_raises_existing_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="fields must contain projection groups")

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")

    # When
    try:
        with pytest.raises(LangfuseQueryError) as error:
            await client.get_scores()
    finally:
        await client.close()

    # Then
    assert error.value.status_code == 400
    assert "projection groups" in error.value.body_snippet


def test_scores_parse_provenance_round_trip() -> None:
    # Given
    comment = (
        '{"scorer":"trajectory","version":"closure-test-v1",'
        '"report_source":"counters","run_ref":"closure-probe"}'
    )

    # When
    provenance = langfuse_query.parse_provenance(comment)

    # Then
    assert provenance == langfuse_query.Provenance(
        scorer="trajectory",
        version="closure-test-v1",
        report_source="counters",
        run_ref="closure-probe",
    )


@pytest.mark.parametrize(
    "comment",
    [None, "", "not-json", "[]", '{"scorer":"trajectory"}'],
)
def test_scores_parse_provenance_returns_none_for_invalid_comment(
    comment: str | None,
) -> None:
    # When
    provenance = langfuse_query.parse_provenance(comment)

    # Then
    assert provenance is None


async def test_scores_closure_probe_comment_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    comment = json.dumps({
        "scorer": "trajectory", "version": "closure-test-v1",
        "report_source": "counters", "run_ref": "closure-probe",
    })

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["name"] == "closure_probe_test"
        return httpx.Response(200, json={
            "data": [{"name": "closure_probe_test", "value": 1.0,
                      "dataType": "NUMERIC", "comment": comment}],
            "meta": {"page": {}},
        })

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse.invalid", "public", "secret")
    try:
        scores, _ = await client.get_scores(name="closure_probe_test")
    finally:
        await client.close()
    assert len(scores) == 1
    assert langfuse_query.parse_provenance(scores[0].comment) == langfuse_query.Provenance(
        scorer="trajectory", version="closure-test-v1",
        report_source="counters", run_ref="closure-probe",
    )


async def test_trace_query_paginates_and_sorts_by_start_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cursors.append(request.url.params.get("cursor"))
        assert request.url.params["traceId"] == TRACE_ID
        if len(cursors) == 1:
            return httpx.Response(
                200,
                json={
                    "data": [_observation(observation_id="later")],
                    "meta": {"cursor": "page-2"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    _observation(
                        observation_id="earlier",
                        start_time="2026-08-17T10:00:00Z",
                    )
                ],
                "meta": {},
            },
        )

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        spans = await LangfuseTraceQuery(client).list_by_trace_id(TRACE_ID)
    finally:
        await client.close()

    assert cursors == [None, "page-2"]
    assert [span.span_id for span in spans] == ["earlier", "later"]


async def test_trace_query_raises_when_cursor_survives_page_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        observation_id = f"page-{request_count}" if request_count <= 2 else ""
        data = [_observation(observation_id=observation_id)] if observation_id else []
        return httpx.Response(200, json={"data": data, "meta": {"cursor": "more"}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        with pytest.raises(LangfuseQueryError) as error:
            await LangfuseTraceQuery(client).list_by_trace_id(TRACE_ID)
    finally:
        await client.close()

    assert request_count == 100
    assert error.value.status_code == 0
    assert "100-page safety cap" in error.value.body_snippet


async def test_trace_query_filters_by_session(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sessionId"] == "session-1"
        assert request.url.params["fromStartTime"] == "2026-08-17T09:00:00+00:00"
        assert request.url.params["toStartTime"] == "2026-08-17T11:00:00+00:00"
        return httpx.Response(200, json={"data": [_observation()], "meta": {}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        spans = await LangfuseTraceQuery(client).list_by_session(
            "session-1",
            from_start_time=datetime(2026, 8, 17, 9, tzinfo=UTC),
            to_start_time=datetime(2026, 8, 17, 11, tzinfo=UTC),
        )
    finally:
        await client.close()

    assert [span.span_id for span in spans] == ["obs-chat"]


async def test_trace_query_passes_time_bounds_for_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fromStartTime"] == "2026-08-17T09:00:00+00:00"
        assert request.url.params["toStartTime"] == "2026-08-17T11:00:00+00:00"
        return httpx.Response(200, json={"data": [], "meta": {}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        spans = await LangfuseTraceQuery(client).list_by_trace_id(
            TRACE_ID,
            from_start_time=datetime(2026, 8, 17, 9, tzinfo=UTC),
            to_start_time=datetime(2026, 8, 17, 11, tzinfo=UTC),
        )
    finally:
        await client.close()

    assert spans == []


async def test_trace_query_reverse_normalizes_tool_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _observation(
        observation_id="obs-tool",
        name="read_file",
        observation_type=LangfuseObservationType.TOOL.value.upper(),
        metadata={
            f"attributes.{GenAiAttr.TOOL_NAME.value}": "read_file",
        },
    )
    payload["output"] = "file contents ok"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [payload], "meta": {}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        spans = await LangfuseTraceQuery(client).list_by_session("session-1")
    finally:
        await client.close()

    assert len(spans) == 1
    assert spans[0].name == SpanName.EXECUTE_TOOL.value
    assert spans[0].attributes[GenAiAttr.TOOL_NAME.value] == "read_file"
    assert spans[0].attributes[GenAiAttr.TOOL_RESULT.value] == "file contents ok"
    assert GenAiAttr.GEN_AI_COMPLETION.value not in spans[0].attributes


async def test_trace_query_prefers_verbatim_usage_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _observation(
        metadata={
            f"attributes.{GenAiAttr.USAGE_INPUT_TOKENS.value}": 120,
        }
    )
    payload["usageDetails"]["input"] = 56

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [payload], "meta": {}})

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        spans = await LangfuseTraceQuery(client).list_by_session("session-1")
    finally:
        await client.close()

    assert spans[0].attributes[GenAiAttr.USAGE_INPUT_TOKENS.value] == 120


async def test_list_sessions_parses_documented_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/v2/sessions"
        assert dict(request.url.params) == {"limit": "25", "page": "2"}
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "session-1", "createdAt": "2026-08-17T10:00:00Z"},
                    {"id": "session-2", "itemsCount": 3},
                ],
                "meta": {"page": 2, "limit": 25, "totalItems": 2, "totalPages": 1},
            },
        )

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        sessions = await client.list_sessions(limit=25, page=2)
    finally:
        await client.close()

    assert [(session.id, session.items_count) for session in sessions] == [
        ("session-1", None),
        ("session-2", 3),
    ]


async def test_http_error_raises_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="sessions endpoint removed in events_only")

    _install_transport(monkeypatch, handler)
    client = LangfuseClient("http://langfuse", "public", "secret")
    try:
        with pytest.raises(LangfuseQueryError) as error:
            await client.list_sessions()
    finally:
        await client.close()

    assert error.value.status_code == 404
    assert "sessions endpoint removed" in error.value.body_snippet
    assert "404" in str(error.value)


def test_observation_to_span_matches_verified_live_mapping() -> None:
    payload = _observation(
        metadata={
            "attributes": {
                "attributes.gen_ai.operation.name": "chat",
                GenAiAttr.REQUEST_MODEL.value: "metadata-model",
                GenAiAttr.GEN_AI_PROMPT.value: "metadata prompt",
                GenAiAttr.GEN_AI_COMPLETION.value: "metadata completion",
                "custom.attribute": "preserved",
            },
            "attributes.gen_ai.output.reasoning_content": "reasoning",
        }
    )
    payload.update(
        {
            "level": "ERROR",
            "statusMessage": "provider failed",
            "providedModelName": "provided-model",
        }
    )
    observation = ObservationData._from_api(payload)

    span = observation_to_span(observation)

    assert span == SpanModel(
        trace_id=TRACE_ID,
        span_id="obs-chat",
        parent_span_id="obs-root",
        name="chat",
        kind=SpanKind.CLIENT.value,
        start_time=datetime(2026, 8, 17, 10, 0, 1, tzinfo=UTC).timestamp(),
        end_time=datetime(2026, 8, 17, 10, 0, 2, tzinfo=UTC).timestamp(),
        attributes={
            GenAiAttr.OPERATION_NAME.value: "chat",
            GenAiAttr.REQUEST_MODEL.value: "metadata-model",
            "custom.attribute": "preserved",
            GenAiAttr.OUTPUT_REASONING_CONTENT.value: "reasoning",
            GenAiAttr.GEN_AI_PROMPT.value: "metadata prompt",
            GenAiAttr.GEN_AI_COMPLETION.value: "metadata completion",
            GenAiAttr.USAGE_INPUT_TOKENS.value: 10,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: 5,
            GenAiAttr.USAGE_TOTAL_TOKENS.value: 15,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 128,
        },
        status=SpanStatus(code=SpanStatusCode.ERROR, message="provider failed"),
    )


def _seed_fixture_trace() -> str:
    """Build two observations for the isolated query response fixture."""
    trace_id = uuid.uuid4().hex
    now = 1_800_000_000.0
    root_span = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        name=SpanName.INVOKE_AGENT.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now,
        end_time=now + 2.0,
        attributes={GenAiAttr.CONVERSATION_ID: "mock-round-trip"},
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    chat_span = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root_span.span_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=now,
        end_time=now + 1.0,
        attributes={
            GenAiAttr.GEN_AI_PROMPT: "user: hello verify",
            GenAiAttr.GEN_AI_COMPLETION: "assistant: verified",
            GenAiAttr.USAGE_INPUT_TOKENS: 10,
            GenAiAttr.USAGE_OUTPUT_TOKENS: 5,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS: 128,
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    _emit_fixture_spans((root_span, chat_span))
    return trace_id


@pytest.fixture
def mock_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    observations: list[dict[str, Any]] = []

    def capture(spans: Sequence[SpanModel]) -> None:
        for span in spans:
            attributes = dict(span.attributes)
            observations.append({
                "id": span.span_id, "traceId": span.trace_id,
                "parentObservationId": span.parent_span_id, "name": span.name,
                "type": attributes.get(GenAiAttr.LANGFUSE_OBSERVATION_TYPE.value, "SPAN"),
                "startTime": datetime.fromtimestamp(span.start_time, UTC).isoformat(),
                "endTime": datetime.fromtimestamp(span.end_time, UTC).isoformat(),
                "input": attributes.get(GenAiAttr.GEN_AI_PROMPT.value),
                "metadata": {f"attributes.{key}": value for key, value in attributes.items()},
                "level": "DEFAULT",
                "usageDetails": {"input": 56},
            })

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "langfuse.invalid"
        return httpx.Response(200, json={"data": observations, "meta": {"cursor": None}})

    monkeypatch.setattr(sys.modules[__name__], "_emit_fixture_spans", capture)
    _install_transport(monkeypatch, handler)


def _emit_fixture_spans(spans: Sequence[SpanModel]) -> None:
    raise AssertionError("mock_langfuse fixture is required")


async def test_langfuse_trace_readback(mock_langfuse: None) -> None:
    trace_id = _seed_fixture_trace()
    client = LangfuseClient("http://langfuse.invalid", "public", "secret")
    try:
        observations, cursor = await client.get_observations(trace_id=trace_id)
    finally:
        await client.close()

    assert cursor is None
    assert len(observations) == 2
    chat = next(observation for observation in observations if observation.name == "chat")
    assert chat.input == "user: hello verify"
    attributes = observation_to_span(chat).attributes
    assert attributes[GenAiAttr.USAGE_INPUT_TOKENS.value] == 10
    assert attributes[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value] == 128


def _seed_fidelity_mapping_session() -> tuple[str, str]:
    """Seed a fresh session whose read-back must reverse-normalize cleanly.

    Reproduces the PRD ticket-6 mapping-gate shape (tool span + chat usage
    delta) with per-run ids, so assertions never depend on stale stored data.
    Returns ``(session_id, trace_id)``.
    """
    trace_id = uuid.uuid4().hex
    session_id = f"fidelity-mapping-{uuid.uuid4().hex[:8]}"
    now = 1_800_000_000.0
    root = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        name=SpanName.INVOKE_AGENT.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now,
        end_time=now + 3.0,
        attributes={
            GenAiAttr.LANGFUSE_INTERNAL_AS_ROOT: True,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.AGENT.value,
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    tool = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root.span_id,
        name=SpanName.EXECUTE_TOOL.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now + 0.5,
        end_time=now + 1.0,
        attributes={
            # Every span carries the session id: v2/observations filters on the
            # observation-level session, which Langfuse only derives from
            # per-span attributes — root-only tagging filters the children out.
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.TOOL.value,
            GenAiAttr.TOOL_NAME: "read_file",
            GenAiAttr.TOOL_RESULT: "file contents ok",
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    chat = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root.span_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=now + 1.0,
        end_time=now + 2.0,
        attributes={
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.GENERATION.value,
            # 120 input with 64 cache-read: Langfuse's native usageDetails.input
            # becomes 120-64=56; the verbatim 120 must win via metadata-first
            # attribute rebuild (PRD ticket-13 allowed-delta list).
            GenAiAttr.USAGE_INPUT_TOKENS: 120,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS: 64,
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    _emit_fixture_spans((root, tool, chat))
    return session_id, trace_id


async def test_fidelity_session_reverse_normalizes_exporter_fields(mock_langfuse: None) -> None:
    session_id, trace_id = _seed_fidelity_mapping_session()
    client = LangfuseClient("http://langfuse.invalid", "public", "secret")
    try:
        spans = await LangfuseTraceQuery(client).list_by_session(session_id)
    finally:
        await client.close()

    tool_spans = [span for span in spans if span.name == SpanName.EXECUTE_TOOL.value]
    chat_span = next(span for span in spans if span.name == SpanName.CHAT.value)
    assert tool_spans
    assert tool_spans[0].attributes[GenAiAttr.TOOL_NAME.value] == "read_file"
    assert tool_spans[0].attributes[GenAiAttr.TOOL_RESULT.value] == "file contents ok"
    assert chat_span.attributes[GenAiAttr.USAGE_INPUT_TOKENS.value] == 120


def _seed_fidelity_export_session() -> tuple[str, str]:
    """Seed a fresh training-relevant trajectory for the SFT export gate.

    Full exporter-relevant shape with per-run ids: root (user input), a
    tool-calling chat, the tool result, a final chat, and the
    ``training_tag`` marker.
    """
    trace_id = uuid.uuid4().hex
    session_id = f"fidelity-export-{uuid.uuid4().hex[:8]}"
    now = 1_800_000_000.0
    root = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        name=SpanName.INVOKE_AGENT.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now,
        end_time=now + 4.0,
        attributes={
            GenAiAttr.LANGFUSE_INTERNAL_AS_ROOT: True,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.AGENT.value,
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.CONVERSATION_ID: session_id,
            GenAiAttr.LANGFUSE_OBSERVATION_INPUT: "read the config and report",
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    tool_chat = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root.span_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=now + 0.5,
        end_time=now + 1.0,
        attributes={
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.GENERATION.value,
            GenAiAttr.OUTPUT_TOOL_CALLS: [
                {"tool_name": "read_file", "arguments": '{"path": "config.yml"}'}
            ],
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    tool = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root.span_id,
        name=SpanName.EXECUTE_TOOL.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now + 1.0,
        end_time=now + 1.5,
        attributes={
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.TOOL.value,
            GenAiAttr.TOOL_NAME: "read_file",
            GenAiAttr.TOOL_RESULT: "key: value",
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    final_chat = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root.span_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=now + 2.0,
        end_time=now + 3.0,
        attributes={
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.GENERATION.value,
            GenAiAttr.OUTPUT_MESSAGES: [
                {"role": "assistant", "parts": [{"type": "text", "content": "config is fine"}]}
            ],
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    training_tag = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=root.span_id,
        name=SpanName.TRAINING_TAG.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now + 3.5,
        end_time=now + 3.6,
        attributes={
            GenAiAttr.LANGFUSE_SESSION_ID: session_id,
            GenAiAttr.TRAINING_RELEVANT: True,
        },
        status=SpanStatus(code=SpanStatusCode.OK),
    )
    _emit_fixture_spans((root, tool_chat, tool, final_chat, training_tag))
    return session_id, trace_id


async def test_training_exporter_over_mock_langfuse(
    tmp_path: Path, mock_langfuse: None,
) -> None:
    session_id, trace_id = _seed_fidelity_export_session()
    client = LangfuseClient("http://langfuse.invalid", "public", "secret")
    query = LangfuseTraceQuery(client)
    exporter = TrainingDataExporter(query, output_dir=tmp_path)
    try:
        spans = await query.list_by_session(session_id)
        assert any(span.name == SpanName.TRAINING_TAG.value for span in spans)
        assert any(
            span.name == SpanName.CHAT.value
            and isinstance(span.attributes.get(GenAiAttr.OUTPUT_TOOL_CALLS.value), list)
            for span in spans
        )
        result = await exporter.export_sft(session_ids=[session_id])
    finally:
        await client.close()

    assert result.sft_count >= 1
    first_example: dict[str, Any] = json.loads(
        result.output_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assistant_message = next(
        message
        for message in first_example["messages"]
        if message.get("role") == MessageRole.ASSISTANT.value and message.get("tool_calls")
    )
    tool_message = next(
        message
        for message in first_example["messages"]
        if message.get("role") == MessageRole.TOOL.value
    )
    assert tool_message["tool_call_id"] == assistant_message["tool_calls"][0]["id"]
