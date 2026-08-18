"""Tests for L2ScoreInjector and ``_build_score_batch``.

Covers:
- batch schema correctness (12 events, score names, NUMERIC dataType, ISO 8601
  timestamp, hex id, optional observationId, has_reasoning as float)
- values are taken verbatim from the caller-supplied ``TrajectoryMetrics``
  (the injector never computes metrics from spans)
- ``inject_scores`` success path (POST URL/body/headers verified)
- fire-and-forget failure paths: network error, non-207, 207-with-errors
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from modex_agent.trace.score_injector import L2ScoreInjector, _build_score_batch
from modex_agent.trace.scoring import TrajectoryMetrics

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


def _make_metrics(*, with_reasoning: bool = True) -> TrajectoryMetrics:
    """Build metrics with known values for assertions."""
    return TrajectoryMetrics(
        tool_success_rate=1.0,
        tool_call_count=1,
        error_tool_count=0,
        iteration_count=1,
        llm_call_count=1,
        total_input_tokens=100,
        total_output_tokens=50,
        total_reasoning_tokens=20 if with_reasoning else 0,
        api_latency_avg_s=2.0,
        cache_hit_rate=0.3,
        response_token_ratio=1 / 3,
        has_reasoning=with_reasoning,
    )


def _zero_metrics() -> TrajectoryMetrics:
    """The zero shape — identical to ``compute_metrics([])``."""
    return TrajectoryMetrics(
        tool_success_rate=1.0,
        tool_call_count=0,
        error_tool_count=0,
        iteration_count=0,
        llm_call_count=0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_reasoning_tokens=0,
        api_latency_avg_s=0.0,
        cache_hit_rate=0.0,
        response_token_ratio=0.0,
        has_reasoning=False,
    )


def _patch_async_client(post_return: object) -> MagicMock:
    """Build an AsyncClient instance mock with a configured POST result."""
    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.aclose = AsyncMock()
    if isinstance(post_return, BaseException):
        mock_client.post = AsyncMock(side_effect=post_return)
    else:
        mock_client.post = AsyncMock(return_value=post_return)
    return mock_client


def _values_by_name(batch: list[dict[str, Any]]) -> dict[str, Any]:
    return {event["body"]["name"]: event["body"]["value"] for event in batch}


# -- _build_score_batch -------------------------------------------------------


def test_build_score_batch_creates_12_events():
    metrics = _make_metrics()

    batch = _build_score_batch(trace_id=_TRACE_ID, metrics=metrics, observation_id=None)

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
    batch_obs = _build_score_batch(
        trace_id=_TRACE_ID, metrics=metrics, observation_id=_OBS_ID
    )
    assert len(batch_obs) == 12
    for event in batch_obs:
        assert event["body"]["observationId"] == _OBS_ID


def test_build_score_batch_values_come_from_metrics():
    """Every event value mirrors the passed metrics field verbatim."""
    metrics = _make_metrics()

    values = _values_by_name(
        _build_score_batch(trace_id=_TRACE_ID, metrics=metrics, observation_id=None)
    )

    assert values["tool_success_rate"] == metrics.tool_success_rate
    assert values["tool_call_count"] == float(metrics.tool_call_count)
    assert values["error_tool_count"] == float(metrics.error_tool_count)
    assert values["iteration_count"] == float(metrics.iteration_count)
    assert values["llm_call_count"] == float(metrics.llm_call_count)
    assert values["total_input_tokens"] == float(metrics.total_input_tokens)
    assert values["total_output_tokens"] == float(metrics.total_output_tokens)
    assert values["total_reasoning_tokens"] == float(metrics.total_reasoning_tokens)
    assert values["api_latency_avg_s"] == metrics.api_latency_avg_s
    assert values["cache_hit_rate"] == metrics.cache_hit_rate
    assert values["response_token_ratio"] == metrics.response_token_ratio
    assert values["has_reasoning"] == float(metrics.has_reasoning)


def test_build_score_batch_zero_metrics_produces_12_events():
    """The zero-shape metrics still produce 12 events (all 0/1.0 values)."""
    values = _values_by_name(
        _build_score_batch(trace_id=_TRACE_ID, metrics=_zero_metrics(), observation_id=None)
    )

    assert set(values) == _EXPECTED_NAMES
    assert values["tool_call_count"] == 0.0
    assert values["tool_success_rate"] == 1.0
    assert values["total_input_tokens"] == 0.0
    assert values["has_reasoning"] == 0.0


def test_build_score_batch_has_reasoning_is_float_not_bool():
    """``has_reasoning`` must be serialized as 0.0/1.0 float, never a bool."""
    # With reasoning → 1.0
    batch_with = _build_score_batch(
        trace_id=_TRACE_ID, metrics=_make_metrics(with_reasoning=True), observation_id=None
    )
    hr_event = next(e for e in batch_with if e["body"]["name"] == "has_reasoning")
    assert isinstance(hr_event["body"]["value"], float)
    assert hr_event["body"]["value"] == 1.0

    # Without reasoning → 0.0
    batch_without = _build_score_batch(
        trace_id=_TRACE_ID, metrics=_make_metrics(with_reasoning=False), observation_id=None
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
    metrics = _make_metrics()

    # Must not raise.
    await injector.inject_scores(_TRACE_ID, metrics)

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert call_args.args == (_URL,)
    assert call_args.kwargs["headers"] == _HEADERS
    body = call_args.kwargs["json"]
    assert "batch" in body
    assert len(body["batch"]) == 12


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_success_passes_observation_id(mock_client_cls):
    mock_client = _patch_async_client(
        MagicMock(
            status_code=207,
            json=lambda: {"successes": [], "errors": []},
        )
    )
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    await injector.inject_scores(_TRACE_ID, _make_metrics(), observation_id=_OBS_ID)

    body = mock_client.post.call_args.kwargs["json"]
    assert all(event["body"]["observationId"] == _OBS_ID for event in body["batch"])


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_reuses_client_across_sequential_posts(mock_client_cls):
    response = MagicMock(
        status_code=207,
        json=lambda: {"successes": [], "errors": []},
    )
    mock_client_cls.return_value = _patch_async_client(response)
    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    await injector.inject_scores("trace-1", _make_metrics())
    await injector.inject_scores("trace-2", _make_metrics())
    await injector.inject_scores("trace-3", _make_metrics())

    mock_client_cls.assert_called_once()


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_aclose_is_idempotent(mock_client_cls):
    mock_client = _patch_async_client(MagicMock(status_code=207, json=lambda: {}))
    mock_client_cls.return_value = mock_client
    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)
    await injector.inject_scores(_TRACE_ID, _make_metrics())

    await injector.aclose()
    await injector.aclose()

    mock_client.aclose.assert_awaited_once()
    assert injector._client is None


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
def test_inject_scores_recreates_client_after_event_loop_restart(mock_client_cls):
    first_client = _patch_async_client(MagicMock(status_code=207, json=lambda: {}))
    second_client = _patch_async_client(MagicMock(status_code=207, json=lambda: {}))
    mock_client_cls.side_effect = [first_client, second_client]
    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    asyncio.run(injector.inject_scores("trace-first-loop", _make_metrics()))
    asyncio.run(injector.inject_scores("trace-second-loop", _make_metrics()))
    asyncio.run(injector.aclose())

    assert mock_client_cls.call_count == 2
    first_client.aclose.assert_awaited_once()
    second_client.post.assert_awaited_once()


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_recreates_client_after_aclose(mock_client_cls):
    first_client = _patch_async_client(MagicMock(status_code=207, json=lambda: {}))
    second_client = _patch_async_client(MagicMock(status_code=207, json=lambda: {}))
    mock_client_cls.side_effect = [first_client, second_client]
    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    await injector.inject_scores("trace-before-close", _make_metrics())
    await injector.aclose()
    await injector.inject_scores("trace-after-close", _make_metrics())
    await injector.aclose()

    assert mock_client_cls.call_count == 2
    second_client.post.assert_awaited_once()


# -- inject_scores: network failure (fire-and-forget) -------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_network_failure(mock_client_cls, caplog):
    mock_client = _patch_async_client(httpx.ConnectError("boom"))
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_metrics())

    assert any("failed to POST scores to Langfuse" in r.message for r in caplog.records)


# -- inject_scores: non-207 response ------------------------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_non_207_response(mock_client_cls, caplog):
    mock_client = _patch_async_client(MagicMock(status_code=500, text="internal server error"))
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_metrics())

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
        await injector.inject_scores(_TRACE_ID, _make_metrics())

    assert any("Langfuse ingestion reported 1 error(s)" in r.message for r in caplog.records)
