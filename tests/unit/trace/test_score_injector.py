"""Tests for L2ScoreInjector and ``_build_score_batch``.

Covers:
- batch schema correctness (12 events, score names, NUMERIC dataType, ISO 8601
  timestamp, hex id, optional observationId, has_reasoning as float)
- ``inject_scores`` success path (POST URL/body/headers verified)
- fire-and-forget failure paths: network error, non-207, 207-with-errors
"""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from modex_agent.trace.score_injector import L2ScoreInjector, _build_score_batch
from modex_agent.trace.semconv import GenAiAttr, SpanName
from modex_agent.trace.store import SpanModel

# -- constants / helpers ------------------------------------------------------

_URL = "https://ingest.example.invalid/api/public/ingestion"
_HEADERS = {"Authorization": "Basic xyz"}
_TRACE_ID = "trace-abc-123"
_OBS_ID = "obs-456"

_LOGGER_NAME = "modex_agent.trace.score_injector"

_EXPECTED_NAMES: frozenset[str] = frozenset(
    {
        "tool_success_rate",
        "tool_call_count",
        "error_tool_count",
        "iteration_count",
        "llm_call_count",
        "total_input_tokens",
        "total_output_tokens",
        "total_reasoning_tokens",
        "api_latency_avg_s",
        "cache_hit_rate",
        "response_token_ratio",
        "has_reasoning",
    }
)


def _make_spans(*, with_reasoning: bool = True) -> list[SpanModel]:
    """Build a minimal span list with known metrics for assertions."""
    chat_attrs: dict[str, object] = {
        GenAiAttr.USAGE_INPUT_TOKENS.value: 100,
        GenAiAttr.USAGE_OUTPUT_TOKENS.value: 50,
        GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 30,
        GenAiAttr.OUTPUT_MESSAGES.value: [
            {"role": "assistant", "parts": [{"type": "text", "content": "done"}]}
        ],
    }
    if with_reasoning:
        chat_attrs[GenAiAttr.USAGE_REASONING_TOKENS.value] = 20
    return [
        SpanModel(
            trace_id=_TRACE_ID,
            span_id="root",
            name=SpanName.INVOKE_AGENT.value,
            start_time=1.0,
            end_time=5.0,
        ),
        SpanModel(
            trace_id=_TRACE_ID,
            span_id="chat-1",
            parent_span_id="root",
            name=SpanName.CHAT.value,
            start_time=1.5,
            end_time=3.5,
            attributes=chat_attrs,
        ),
        SpanModel(
            trace_id=_TRACE_ID,
            span_id="tool-1",
            parent_span_id="root",
            name=SpanName.EXECUTE_TOOL.value,
            start_time=2.0,
            end_time=2.5,
        ),
    ]


def _patch_async_client(post_return: object) -> MagicMock:
    """Build an AsyncClient instance mock usable as ``async with``."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    if isinstance(post_return, BaseException):
        mock_client.post = AsyncMock(side_effect=post_return)
    else:
        mock_client.post = AsyncMock(return_value=post_return)
    return mock_client


# -- _build_score_batch -------------------------------------------------------


def test_build_score_batch_creates_12_events():
    spans = _make_spans()

    batch = _build_score_batch(trace_id=_TRACE_ID, spans=spans, observation_id=None)

    assert isinstance(batch, list)
    assert len(batch) == 12

    actual_names = {event["body"]["name"] for event in batch}
    assert actual_names == _EXPECTED_NAMES

    for event in batch:
        assert isinstance(event["id"], str)
        assert event["id"]
        assert set(event["id"]) <= set("0123456789abcdef")

        assert event["type"] == "score-create"

        assert isinstance(event["timestamp"], str)
        datetime.fromisoformat(event["timestamp"])

        body = event["body"]
        assert isinstance(body["id"], str)
        assert body["id"]
        assert body["traceId"] == _TRACE_ID
        assert isinstance(body["name"], str)
        assert isinstance(body["value"], float)
        assert body["dataType"] == "NUMERIC"
        assert "observationId" not in body

    # With observation_id — observationId must be present in every body.
    batch_obs = _build_score_batch(trace_id=_TRACE_ID, spans=spans, observation_id=_OBS_ID)
    assert len(batch_obs) == 12
    for event in batch_obs:
        assert event["body"]["observationId"] == _OBS_ID


def test_build_score_batch_empty_spans_produces_12_events():
    """Empty span list still produces 12 events (all metrics default to 0/1.0)."""
    batch = _build_score_batch(trace_id=_TRACE_ID, spans=[], observation_id=None)

    assert len(batch) == 12
    actual_names = {event["body"]["name"] for event in batch}
    assert actual_names == _EXPECTED_NAMES


def test_build_score_batch_has_reasoning_is_float_not_bool():
    """``has_reasoning`` must be serialized as 0.0/1.0 float, never a bool."""
    # With reasoning → 1.0
    batch_with = _build_score_batch(
        trace_id=_TRACE_ID, spans=_make_spans(with_reasoning=True), observation_id=None
    )
    hr_event = next(e for e in batch_with if e["body"]["name"] == "has_reasoning")
    assert isinstance(hr_event["body"]["value"], float)
    assert hr_event["body"]["value"] == 1.0

    # Without reasoning → 0.0
    batch_without = _build_score_batch(
        trace_id=_TRACE_ID, spans=_make_spans(with_reasoning=False), observation_id=None
    )
    hr_event = next(e for e in batch_without if e["body"]["name"] == "has_reasoning")
    assert isinstance(hr_event["body"]["value"], float)
    assert hr_event["body"]["value"] == 0.0


# -- inject_scores: success ---------------------------------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_success(mock_client_cls):
    mock_client = _patch_async_client(
        MagicMock(
            status_code=207,
            json=lambda: {"successes": [], "errors": []},
        )
    )
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)
    spans = _make_spans()

    # Must not raise.
    await injector.inject_scores(_TRACE_ID, spans)

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert call_args.args == (_URL,)
    assert call_args.kwargs["headers"] == _HEADERS
    body = call_args.kwargs["json"]
    assert "batch" in body
    assert len(body["batch"]) == 12


# -- inject_scores: network failure (fire-and-forget) -------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_network_failure(mock_client_cls, caplog):
    mock_client = _patch_async_client(httpx.ConnectError("boom"))
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_spans())

    assert any("failed to POST scores to Langfuse" in r.message for r in caplog.records)


# -- inject_scores: non-207 response ------------------------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_non_207_response(mock_client_cls, caplog):
    mock_client = _patch_async_client(MagicMock(status_code=500, text="internal server error"))
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_spans())

    assert any("Langfuse ingestion returned HTTP 500" in r.message for r in caplog.records)


# -- inject_scores: 207 with per-event errors ---------------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_207_with_errors(mock_client_cls, caplog):
    errors = [{"id": "evt-1", "message": "bad score"}]
    mock_client = _patch_async_client(
        MagicMock(
            status_code=207,
            json=lambda: {"successes": [], "errors": errors},
        )
    )
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_spans())

    assert any("Langfuse ingestion reported 1 error(s)" in r.message for r in caplog.records)
