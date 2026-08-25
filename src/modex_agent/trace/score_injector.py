"""L2 score injector — push heuristic trajectory scores to Langfuse.

Posts ``score-create`` events to the Langfuse ingestion API
(``POST {host}/api/public/ingestion``) so that L2 heuristic trajectory
metrics (``TrajectoryMetrics``, accumulated by
:class:`modex_agent.trace.session_state.TraceSessionState` counters) appear
as NUMERIC scores on the corresponding Langfuse trace.

Fire-and-forget by design: every failure path is logged as a warning and
swallowed. A score-posting failure must never break the turn.
"""

# allow: SIZE_OK — score construction and posting lifecycle must share one path.

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from modex_agent.trace.scoring import TrajectoryMetrics

logger = logging.getLogger(__name__)

INJECTOR_VERSION: Final = "v1"

# 5-second budget covers connect + write + read for a single small batch POST.
_INJECT_TIMEOUT = httpx.Timeout(5.0)
_CLOSE_GRACE_SECONDS = 5.0
_MAX_COMMENT_CHARS: Final = 4000

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


class ScoreSpec(BaseModel):
    """A named Langfuse score and its optional provenance comment."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(min_length=1)
    value: float | bool
    data_type: Literal["NUMERIC", "BOOLEAN"]
    comment: str | None = None


class _TrajectoryProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["trajectory"] = "trajectory"
    version: str = INJECTOR_VERSION
    report_source: Literal["counters"] = "counters"
    run_ref: str


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
        session_id: str | None = None,
        extra_scores: list[ScoreSpec] | None = None,
    ) -> None:
        """Inject trajectory metrics and optional scores in one batch.

        Reuses a resident :class:`httpx.AsyncClient` (5s timeout), POSTs the
        batch, logs a warning on any failure. Never raises.
        """
        run_ref = session_id if session_id is not None else trace_id
        comment = _TrajectoryProvenance(run_ref=run_ref).model_dump_json()
        scores = [
            *_trajectory_score_specs(metrics, comment=comment),
            *(extra_scores or []),
        ]
        await self.inject_score_batch(
            trace_id,
            scores,
            observation_id=observation_id,
        )

    async def inject_score_batch(
        self,
        trace_id: str,
        scores: list[ScoreSpec],
        *,
        observation_id: str | None = None,
    ) -> None:
        """Inject arbitrary named scores without propagating Langfuse failures."""
        current_task = asyncio.current_task()
        if current_task is not None:
            self._in_flight.add(current_task)

        guarded_scores: list[ScoreSpec] = []
        for score in scores:
            comment = score.comment
            if comment is not None and len(comment) > _MAX_COMMENT_CHARS:
                logger.warning(
                    "L2ScoreInjector: truncated score comment to %d characters "
                    "(trace_id=%s, score_name=%s)",
                    _MAX_COMMENT_CHARS,
                    trace_id,
                    score.name,
                )
                score = score.model_copy(
                    update={"comment": comment[:_MAX_COMMENT_CHARS]}
                )
            guarded_scores.append(score)

        batch = _build_named_score_batch(
            trace_id=trace_id,
            scores=guarded_scores,
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
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build the 12 trajectory ``score-create`` events."""
    run_ref = session_id if session_id is not None else trace_id
    comment = _TrajectoryProvenance(run_ref=run_ref).model_dump_json()
    return _build_named_score_batch(
        trace_id=trace_id,
        scores=_trajectory_score_specs(metrics, comment=comment),
        observation_id=observation_id,
    )


def _trajectory_score_specs(
    metrics: TrajectoryMetrics,
    *,
    comment: str,
) -> list[ScoreSpec]:
    values: dict[str, float] = {
        "tool_success_rate": metrics.tool_success_rate,
        "tool_call_count": float(metrics.tool_call_count),
        "error_tool_count": float(metrics.error_tool_count),
        "iteration_count": float(metrics.iteration_count),
        "llm_call_count": float(metrics.llm_call_count),
        "total_input_tokens": float(metrics.total_input_tokens),
        "total_output_tokens": float(metrics.total_output_tokens),
        "total_reasoning_tokens": float(metrics.total_reasoning_tokens),
        "api_latency_avg_s": metrics.api_latency_avg_s,
        "cache_hit_rate": metrics.cache_hit_rate,
        "response_token_ratio": metrics.response_token_ratio,
        "has_reasoning": float(metrics.has_reasoning),
    }
    return [
        ScoreSpec(
            name=name,
            value=values[name],
            data_type="NUMERIC",
            comment=comment,
        )
        for name in _SCORE_NAMES
    ]


def _build_named_score_batch(
    *,
    trace_id: str,
    scores: list[ScoreSpec],
    observation_id: str | None,
) -> list[dict[str, Any]]:
    """Build the ``batch`` array of arbitrary ``score-create`` events.

    Each event carries a top-level ``timestamp`` (REQUIRED by the Langfuse
    ingestion API — omitting it yields HTTP 400) and a ``body`` with
    score identity, value, type, optional provenance comment, and optional
    observation linkage.
    """
    timestamp = datetime.now(UTC).isoformat()
    batch: list[dict[str, Any]] = []
    for score in scores:
        value: float | int | str = score.value
        if score.data_type == "BOOLEAN" and isinstance(value, bool):
            value = int(value)
        body: dict[str, Any] = {
            "id": uuid4().hex,
            "traceId": trace_id,
            "name": score.name,
            "value": value,
            "dataType": score.data_type,
        }
        if observation_id is not None:
            body["observationId"] = observation_id
        if score.comment is not None:
            body["comment"] = score.comment
        batch.append(
            {
                "id": uuid4().hex,
                "type": "score-create",
                "timestamp": timestamp,
                "body": body,
            }
        )
    return batch
