"""L2 score injector — push heuristic trajectory scores to Langfuse.

Posts ``score-create`` events to the Langfuse ingestion API
(``POST {host}/api/public/ingestion``) so that L2 heuristic scores computed by
:mod:`modex_agent.trace.scoring` appear as NUMERIC scores on the corresponding
Langfuse trace.

Fire-and-forget by design: every failure path is logged as a warning and
swallowed. A score-posting failure must never break the turn.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from modex_agent.trace.scoring import TrajectoryScore, overall_score

logger = logging.getLogger(__name__)

# 5-second budget covers connect + write + read for a single small batch POST.
_INJECT_TIMEOUT = httpx.Timeout(5.0)

# Langfuse score names — order is stable for readability.
_SCORE_NAMES: tuple[str, ...] = (
    "tool_success_rate",
    "reasoning_depth",
    "trajectory_compactness",
    "overall",
)


class L2ScoreInjector:
    """Inject L2 heuristic scores into Langfuse via the ingestion API.

    Fire-and-forget: failures are logged as warnings and never propagated.
    Called after the root span is emitted.
    """

    def __init__(self, *, ingestion_url: str, headers: dict[str, str]) -> None:
        """Store URL + headers.

        No HTTP client is created here — :meth:`inject_scores` opens a fresh
        :class:`httpx.AsyncClient` per call so there is no lifecycle to manage
        and no shared connection state across turns.
        """
        self._ingestion_url = ingestion_url
        self._headers = headers

    async def inject_scores(
        self,
        trace_id: str,
        scores: TrajectoryScore,
        *,
        observation_id: str | None = None,
    ) -> None:
        """Inject 4 NUMERIC scores: ``tool_success_rate``, ``reasoning_depth``,
        ``trajectory_compactness``, and ``overall``.

        Uses :func:`overall_score` from :mod:`modex_agent.trace.scoring` to
        compute the combined score.  Creates a fresh
        :class:`httpx.AsyncClient` (5s timeout), POSTs the batch, logs a
        warning on any failure.  Never raises.
        """
        batch = _build_score_batch(
            trace_id=trace_id,
            scores=scores,
            observation_id=observation_id,
        )
        try:
            async with httpx.AsyncClient(timeout=_INJECT_TIMEOUT) as client:
                response = await client.post(
                    self._ingestion_url,
                    json={"batch": batch},
                    headers=self._headers,
                )
        except Exception:
            logger.warning("L2ScoreInjector: failed to POST scores to Langfuse (trace_id=%s)", trace_id)
            return

        if response.status_code != 207:
            logger.warning(
                "L2ScoreInjector: Langfuse ingestion returned HTTP %s (trace_id=%s): %s",
                response.status_code,
                trace_id,
                response.text[:200],
            )
            return

        # 207 Multi-Status — still need to inspect per-event errors.
        try:
            body = response.json()
        except Exception:
            logger.warning(
                "L2ScoreInjector: Langfuse 207 response body was not JSON (trace_id=%s): %s",
                trace_id,
                response.text[:200],
            )
            return

        errors = body.get("errors") if isinstance(body, dict) else None
        if isinstance(errors, list) and errors:
            logger.warning(
                "L2ScoreInjector: Langfuse ingestion reported %d error(s) (trace_id=%s): %s",
                len(errors),
                trace_id,
                errors,
            )


def _build_score_batch(
    *,
    trace_id: str,
    scores: TrajectoryScore,
    observation_id: str | None,
) -> list[dict[str, Any]]:
    """Build the ``batch`` array of ``score-create`` events.

    Each event carries a top-level ``timestamp`` (REQUIRED by the Langfuse
    ingestion API — omitting it yields HTTP 400) and a ``body`` with
    ``traceId``, ``name``, ``value``, ``dataType="NUMERIC"``, plus
    ``observationId`` when one is supplied.
    """
    values: dict[str, float] = {
        "tool_success_rate": scores.tool_success_rate,
        "reasoning_depth": float(scores.reasoning_depth),
        "trajectory_compactness": scores.trajectory_compactness,
        "overall": overall_score(scores),
    }
    timestamp = datetime.now(UTC).isoformat()
    batch: list[dict[str, Any]] = []
    for name in _SCORE_NAMES:
        body: dict[str, Any] = {
            "id": uuid4().hex,
            "traceId": trace_id,
            "name": name,
            "value": values[name],
            "dataType": "NUMERIC",
        }
        if observation_id is not None:
            body["observationId"] = observation_id
        batch.append(
            {
                "id": uuid4().hex,
                "type": "score-create",
                "timestamp": timestamp,
                "body": body,
            }
        )
    return batch
