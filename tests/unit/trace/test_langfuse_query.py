from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from modex_agent.trace.langfuse_query import (
    LangfuseClient,
    LangfuseQueryError,
    LangfuseTraceQuery,
    ObservationData,
    observation_to_span,
)
from modex_agent.trace.otel_store import _emit_span_via_json_otlp
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanModel, SpanStatus
from modex_agent.trace.training_exporter import MessageRole, TrainingDataExporter

TRACE_ID = "0123456789abcdef0123456789abc003"
_LIVE_LANGFUSE_CONFIGURED = bool(
    os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
)
_LIVE_COLLECTOR_ENDPOINT = os.environ.get("OTEL_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
_LIVE_INGEST_TIMEOUT_S = 30.0


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


def _live_client() -> LangfuseClient:
    return LangfuseClient(
        os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        os.environ["LANGFUSE_PUBLIC_KEY"],
        os.environ["LANGFUSE_SECRET_KEY"],
    )


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


def _seed_live_trace() -> str:
    """Seed a fresh two-span trace through the live OTLP collector.

    Current timestamps keep the data clear of the 180-day ClickHouse TTL
    (which organically expired the previously hard-coded 2025-dated trace),
    and the per-run trace id keeps assertions off stale data.
    """
    trace_id = uuid.uuid4().hex
    now = time.time()
    root_span = SpanModel(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex[:16],
        name=SpanName.INVOKE_AGENT.value,
        kind=SpanKind.INTERNAL.value,
        start_time=now,
        end_time=now + 2.0,
        attributes={GenAiAttr.CONVERSATION_ID: "live-round-trip"},
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
    _emit_live_spans((root_span, chat_span))
    return trace_id


def _emit_live_spans(spans: Sequence[SpanModel]) -> None:
    """Push spans through the live collector endpoint (round-trip path)."""
    with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
        for span in spans:
            _emit_span_via_json_otlp(client, _LIVE_COLLECTOR_ENDPOINT, {}, "modex_agent", span)


async def _await_live_ingest(client: LangfuseClient, trace_id: str, expected: int) -> None:
    """Poll until the trace's observations are queryable (v4 async ingest)."""
    deadline = time.monotonic() + _LIVE_INGEST_TIMEOUT_S
    while time.monotonic() < deadline:
        observations, _ = await client.get_observations(trace_id=trace_id)
        if len(observations) >= expected:
            return
        await asyncio.sleep(1.0)
    pytest.fail(
        f"live ingest timeout: {expected} observations for trace {trace_id} "
        f"not queryable within {_LIVE_INGEST_TIMEOUT_S}s"
    )


@pytest.mark.skipif(
    not _LIVE_LANGFUSE_CONFIGURED,
    reason="Langfuse credentials are not configured",
)
async def test_live_langfuse_trace_round_trip() -> None:
    trace_id = _seed_live_trace()
    client = _live_client()
    try:
        deadline = time.monotonic() + _LIVE_INGEST_TIMEOUT_S
        observations: list[ObservationData] = []
        cursor: str | None = None
        while time.monotonic() < deadline:
            observations, cursor = await client.get_observations(trace_id=trace_id)
            if len(observations) >= 2:
                break
            await asyncio.sleep(1.0)
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
    now = time.time()
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
    _emit_live_spans((root, tool, chat))
    return session_id, trace_id


@pytest.mark.skipif(
    not _LIVE_LANGFUSE_CONFIGURED,
    reason="Langfuse credentials are not configured",
)
async def test_live_fidelity_session_reverse_normalizes_exporter_fields() -> None:
    session_id, trace_id = _seed_fidelity_mapping_session()
    client = _live_client()
    try:
        await _await_live_ingest(client, trace_id, expected=3)
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
    now = time.time()
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
    _emit_live_spans((root, tool_chat, tool, final_chat, training_tag))
    return session_id, trace_id


@pytest.mark.skipif(
    not _LIVE_LANGFUSE_CONFIGURED,
    reason="Langfuse credentials are not configured",
)
async def test_live_training_exporter_over_langfuse(
    tmp_path: Path,
) -> None:
    session_id, trace_id = _seed_fidelity_export_session()
    client = _live_client()
    query = LangfuseTraceQuery(client)
    exporter = TrainingDataExporter(query, output_dir=tmp_path)
    try:
        await _await_live_ingest(client, trace_id, expected=5)
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
