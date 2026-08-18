"""L2 score injector — push heuristic trajectory scores to Langfuse.

Posts ``score-create`` events to the Langfuse ingestion API
(``POST {host}/api/public/ingestion``) so that L2 heuristic trajectory
metrics (``TrajectoryMetrics``, accumulated by
:class:`modex_agent.trace.session_state.TraceSessionState` counters) appear
as NUMERIC scores on the corresponding Langfuse trace.

Fire-and-forget by design: every failure path is logged as a warning and
swallowed. A score-posting failure must never break the turn.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx

from modex_agent.trace.scoring import TrajectoryMetrics

logger = logging.getLogger(__name__)

# 5-second budget covers connect + write + read for a single small batch POST.
_INJECT_TIMEOUT = httpx.Timeout(5.0)
_CLOSE_GRACE_SECONDS = 5.0

# Langfuse score names — order is stable for readability.
_SCORE_NAMES: tuple[str, ...] = (
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
)


class L2ScoreInjector:
    """Inject L2 heuristic scores into Langfuse via the ingestion API.

    Fire-and-forget: failures are logged as warnings and never propagated.
    Called after the root span is emitted. The resident HTTP client is created
    lazily on the first injection and belongs to the event loop that created
    it. Reusing the injector from another loop closes that stale client and
    creates a new loop-local client before posting.
    """

    def __init__(self, *, ingestion_url: str, headers: dict[str, str]) -> None:
        """Store URL + headers without creating an event-loop-bound client."""
        self._ingestion_url = ingestion_url
        self._headers = headers
        self._client: httpx.AsyncClient | None = None
        self._client_loop: asyncio.AbstractEventLoop | None = None
        self._in_flight: set[asyncio.Task[None]] = set()

    async def _close_client(self, client: httpx.AsyncClient) -> None:
        try:
            await client.aclose()
        except Exception:
            logger.debug("L2ScoreInjector: resident client close failed", exc_info=True)

    async def _ensure_client(self) -> httpx.AsyncClient:
        running_loop = asyncio.get_running_loop()
        client = self._client
        if (
            client is not None
            and not client.is_closed
            and self._client_loop is running_loop
        ):
            return client

        if client is not None:
            self._client = None
            self._client_loop = None
            await self._close_client(client)

        client = self._client
        if (
            client is not None
            and not client.is_closed
            and self._client_loop is running_loop
        ):
            return client

        client = httpx.AsyncClient(timeout=_INJECT_TIMEOUT)
        self._client = client
        self._client_loop = running_loop
        return client

    async def _post_batch(self, batch: list[dict[str, Any]]) -> httpx.Response:
        client = await self._ensure_client()
        try:
            return await client.post(
                self._ingestion_url,
                json={"batch": batch},
                headers=self._headers,
            )
        except Exception:
            if (
                not client.is_closed
                and self._client_loop is asyncio.get_running_loop()
            ):
                raise
            replacement = await self._ensure_client()
            return await replacement.post(
                self._ingestion_url,
                json={"batch": batch},
                headers=self._headers,
            )

    async def aclose(self) -> None:
        """Drain active score posts briefly, then close the resident client."""
        current_task = asyncio.current_task()
        pending = {
            task
            for task in self._in_flight
            if task is not current_task and not task.done()
        }
        if pending:
            await asyncio.wait(pending, timeout=_CLOSE_GRACE_SECONDS)

        client = self._client
        self._client = None
        self._client_loop = None
        if client is not None:
            await self._close_client(client)

    async def inject_scores(
        self,
        trace_id: str,
        metrics: TrajectoryMetrics,
        *,
        observation_id: str | None = None,
    ) -> None:
        """Inject 12 NUMERIC scores derived from ``metrics``.

        Reuses a resident :class:`httpx.AsyncClient` (5s timeout), POSTs the
        batch, logs a warning on any failure. Never raises.
        """
        current_task = asyncio.current_task()
        if current_task is not None:
            self._in_flight.add(current_task)
        batch = _build_score_batch(
            trace_id=trace_id,
            metrics=metrics,
            observation_id=observation_id,
        )
        try:
            response = await self._post_batch(batch)
        except Exception:
            logger.warning(
                "L2ScoreInjector: failed to POST scores to Langfuse (trace_id=%s)", trace_id
            )
            return
        finally:
            if current_task is not None:
                self._in_flight.discard(current_task)

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
    metrics: TrajectoryMetrics,
    observation_id: str | None,
) -> list[dict[str, Any]]:
    """Build the ``batch`` array of ``score-create`` events.

    Each event carries a top-level ``timestamp`` (REQUIRED by the Langfuse
    ingestion API — omitting it yields HTTP 400) and a ``body`` with
    ``traceId``, ``name``, ``value``, ``dataType="NUMERIC"``, plus
    ``observationId`` when one is supplied.
    """
    scores = metrics
    values: dict[str, float] = {
        "tool_success_rate": scores.tool_success_rate,
        "tool_call_count": float(scores.tool_call_count),
        "error_tool_count": float(scores.error_tool_count),
        "iteration_count": float(scores.iteration_count),
        "llm_call_count": float(scores.llm_call_count),
        "total_input_tokens": float(scores.total_input_tokens),
        "total_output_tokens": float(scores.total_output_tokens),
        "total_reasoning_tokens": float(scores.total_reasoning_tokens),
        "api_latency_avg_s": scores.api_latency_avg_s,
        "cache_hit_rate": scores.cache_hit_rate,
        "response_token_ratio": scores.response_token_ratio,
        "has_reasoning": float(scores.has_reasoning),
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
