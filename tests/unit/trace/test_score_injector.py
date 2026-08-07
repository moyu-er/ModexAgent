"""Tests for L2ScoreInjector and ``_build_score_batch``.

Covers:
- batch schema correctness (4 events, score names, NUMERIC dataType, ISO 8601
  timestamp, hex id, optional observationId, reasoning_depth as float)
- ``inject_scores`` success path (POST URL/body/headers verified)
- fire-and-forget failure paths: network error, non-207, 207-with-errors
"""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from modex_agent.trace.score_injector import L2ScoreInjector, _build_score_batch
from modex_agent.trace.scoring import TrajectoryScore

# -- constants / helpers ------------------------------------------------------

_URL = "https://ingest.example.invalid/api/public/ingestion"
_HEADERS = {"Authorization": "Basic xyz"}
_TRACE_ID = "trace-abc-123"
_OBS_ID = "obs-456"

_LOGGER_NAME = "modex_agent.trace.score_injector"


def _make_scores() -> TrajectoryScore:
    """Deterministic TrajectoryScore for assertions."""
    return TrajectoryScore(
        tool_success_rate=0.8,
        reasoning_depth=42,
        trajectory_compactness=0.15,
    )


def _patch_async_client(post_return: object) -> MagicMock:
    """Build an AsyncClient instance mock usable as ``async with``.

    ``post_return`` is either a response-like MagicMock (returned by .post) or
    an Exception instance (raised by .post).
    """
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    if isinstance(post_return, BaseException):
        mock_client.post = AsyncMock(side_effect=post_return)
    else:
        mock_client.post = AsyncMock(return_value=post_return)
    return mock_client


# -- _build_score_batch -------------------------------------------------------


def test_build_score_batch_creates_4_events():
    scores = _make_scores()

    # Without observation_id — observationId must be absent.
    batch = _build_score_batch(trace_id=_TRACE_ID, scores=scores, observation_id=None)

    assert isinstance(batch, list)
    assert len(batch) == 4

    expected_names = {
        "tool_success_rate",
        "reasoning_depth",
        "trajectory_compactness",
        "overall",
    }
    actual_names = {event["body"]["name"] for event in batch}
    assert actual_names == expected_names

    for event in batch:
        # id: non-empty hex string
        assert isinstance(event["id"], str)
        assert event["id"]
        assert set(event["id"]) <= set("0123456789abcdef")

        assert event["type"] == "score-create"

        # timestamp: ISO 8601 parseable string
        assert isinstance(event["timestamp"], str)
        datetime.fromisoformat(event["timestamp"])

        body = event["body"]
        # body.id is REQUIRED by Langfuse v4 — without it the score is
        # accepted (207/201) but never materialized to the scores table.
        assert isinstance(body["id"], str)
        assert body["id"]
        assert body["traceId"] == _TRACE_ID
        assert isinstance(body["name"], str)
        assert isinstance(body["value"], float)
        assert body["dataType"] == "NUMERIC"
        assert "observationId" not in body

    # reasoning_depth value is a float, even though TrajectoryScore stores int.
    rd_event = next(
        e for e in batch if e["body"]["name"] == "reasoning_depth"
    )
    assert isinstance(rd_event["body"]["value"], float)
    assert rd_event["body"]["value"] == 42.0

    # With observation_id — observationId must be present in every body.
    batch_obs = _build_score_batch(
        trace_id=_TRACE_ID, scores=scores, observation_id=_OBS_ID
    )
    assert len(batch_obs) == 4
    for event in batch_obs:
        assert event["body"]["observationId"] == _OBS_ID


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

    # Must not raise.
    await injector.inject_scores(_TRACE_ID, _make_scores())

    mock_client.post.assert_awaited_once()
    call_args = mock_client.post.call_args
    assert call_args.args == (_URL,)
    assert call_args.kwargs["headers"] == _HEADERS
    body = call_args.kwargs["json"]
    assert "batch" in body
    assert len(body["batch"]) == 4


# -- inject_scores: network failure (fire-and-forget) -------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_network_failure(mock_client_cls, caplog):
    mock_client = _patch_async_client(httpx.ConnectError("boom"))
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_scores())

    assert any(
        "failed to POST scores to Langfuse" in r.message for r in caplog.records
    )


# -- inject_scores: non-207 response ------------------------------------------


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_inject_scores_non_207_response(mock_client_cls, caplog):
    mock_client = _patch_async_client(
        MagicMock(status_code=500, text="internal server error")
    )
    mock_client_cls.return_value = mock_client

    injector = L2ScoreInjector(ingestion_url=_URL, headers=_HEADERS)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        # Must not raise.
        await injector.inject_scores(_TRACE_ID, _make_scores())

    assert any(
        "Langfuse ingestion returned HTTP 500" in r.message for r in caplog.records
    )


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
        await injector.inject_scores(_TRACE_ID, _make_scores())

    assert any(
        "Langfuse ingestion reported 1 error(s)" in r.message
        for r in caplog.records
    )
