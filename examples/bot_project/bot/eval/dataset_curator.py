"""Dataset curator — build eval datasets from production Langfuse traces.

Queries Langfuse v4 traces via the REST API (the Langfuse SDK has no trace
query method), filters for interesting cases (errors, high latency), and
creates dataset items with ``source_trace_id`` linkage via the SDK.

Layer 2 of the eval architecture (ADR-0024, IN15 step 6): flagged production
traces → Langfuse dataset → eval runner. Runs as a separate process (opt-in
via the ``[eval]`` extra) to avoid OTel tracer-provider conflicts with the
bot's JSON-OTLP trace path.

Usage::

    curator = DatasetCurator(
        langfuse_host="http://localhost:3000",
        public_key="pk-lf-...",
        secret_key="sk-lf-...",
    )
    count = await curator.curate(dataset_name="react-baseline", max_items=50)
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from langfuse import Langfuse

logger = logging.getLogger(__name__)

# 10s budget covers connect + read for trace/observation list queries, which
# may return sizeable payloads.
_QUERY_TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True)
class TraceSummary:
    """Minimal trace info for curation decisions."""

    trace_id: str
    session_id: str
    name: str
    observation_count: int
    has_error: bool
    latency_ms: float


class DatasetCurator:
    """Curate eval datasets from production Langfuse traces.

    Queries traces via REST API (the Langfuse SDK has no trace query method),
    filters for interesting cases (errors, high latency, high iteration count),
    and creates dataset items with ``source_trace_id`` linkage via the SDK.
    """

    def __init__(
        self,
        *,
        langfuse_host: str,
        public_key: str,
        secret_key: str,
        langfuse_client: Langfuse | None = None,
    ) -> None:
        """Store host + Basic auth header; build or reuse a Langfuse client.

        A fresh :class:`httpx.AsyncClient` is opened per query call (no shared
        connection state to manage), mirroring :mod:`modex_agent.trace.score_injector`.
        """
        self._host = langfuse_host.rstrip("/")
        self._auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._headers = {"Authorization": f"Basic {self._auth}"}
        self._lf = langfuse_client or Langfuse(
            host=self._host,
            public_key=public_key,
            secret_key=secret_key,
        )

    async def list_traces(
        self,
        *,
        limit: int = 50,
        session_id: str | None = None,
    ) -> list[TraceSummary]:
        """Fetch recent traces from Langfuse, return summaries for curation.

        ``has_error`` is derived from the trace-level ``level`` field (Langfuse
        v4 propagates the worst observation level to the trace), avoiding an
        N+1 observation query per trace.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "orderBy": "timestamp_desc",
        }
        if session_id is not None:
            params["sessionId"] = session_id
        url = f"{self._host}/api/public/traces"
        try:
            async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=self._headers)
        except Exception:
            logger.warning("DatasetCurator: failed to list traces", exc_info=True)
            return []

        if response.status_code != 200:
            logger.warning(
                "DatasetCurator: GET /api/public/traces returned HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
            return []

        try:
            body = response.json()
        except Exception:
            logger.warning(
                "DatasetCurator: traces response was not JSON: %s",
                response.text[:200],
            )
            return []

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            logger.warning("DatasetCurator: traces response 'data' was not a list")
            return []

        summaries: list[TraceSummary] = []
        for trace in data:
            if not isinstance(trace, dict):
                continue
            trace_id = trace.get("id")
            if not isinstance(trace_id, str):
                continue
            summaries.append(
                TraceSummary(
                    trace_id=trace_id,
                    session_id=trace.get("sessionId") or "",
                    name=trace.get("name") or "",
                    observation_count=int(trace.get("observationCount") or 0),
                    has_error=trace.get("level") == "ERROR",
                    latency_ms=float(trace.get("latency") or 0.0),
                )
            )
        return summaries

    async def fetch_trace_io(self, trace_id: str) -> dict[str, Any] | None:
        """Fetch the input/output of a trace by querying its observations.

        Finds the root observation (``parentObservationId`` is None, preferring
        ``type=AGENT`` / ``name=invoke_agent``) and extracts its ``input`` and
        ``output`` fields. Returns ``None`` if the trace is not found or has no
        root observation.
        """
        filter_payload = json.dumps(
            [
                {
                    "type": "string",
                    "column": "traceId",
                    "operator": "=",
                    "value": trace_id,
                }
            ]
        )
        url = f"{self._host}/api/public/v2/observations"
        params: dict[str, Any] = {
            "fields": "core,basic,io",
            "limit": 50,
            "filter": filter_payload,
        }
        try:
            async with httpx.AsyncClient(timeout=_QUERY_TIMEOUT) as client:
                response = await client.get(url, params=params, headers=self._headers)
        except Exception:
            logger.warning(
                "DatasetCurator: failed to fetch observations (trace_id=%s)",
                trace_id,
                exc_info=True,
            )
            return None

        if response.status_code != 200:
            logger.warning(
                "DatasetCurator: GET /api/public/v2/observations returned HTTP %s"
                " (trace_id=%s): %s",
                response.status_code,
                trace_id,
                response.text[:200],
            )
            return None

        try:
            body = response.json()
        except Exception:
            logger.warning(
                "DatasetCurator: observations response was not JSON (trace_id=%s)",
                trace_id,
            )
            return None

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return None

        root = _find_root_observation(data)
        if root is None:
            return None
        return {
            "input": root.get("input"),
            "output": root.get("output"),
            "trace_id": trace_id,
        }

    async def curate(
        self,
        *,
        dataset_name: str,
        description: str = "",
        max_items: int = 50,
        filter_errors: bool = True,
        filter_high_latency: bool = False,
        latency_threshold_ms: float = 10000,
    ) -> int:
        """Curate a dataset from production traces.

        1. Ensure the dataset exists (create if needed — idempotent).
        2. List recent traces (3x ``max_items`` to account for filtering).
        3. Filter by enabled criteria (OR semantics: keep if any matches).
        4. For each interesting trace, fetch its I/O.
        5. Create a dataset item with ``source_trace_id`` linkage.

        Returns the number of dataset items created. One bad trace never stops
        the whole curation — failures are logged as warnings.
        """
        if max_items <= 0:
            return 0

        # 1. Ensure dataset exists (idempotent — returns the existing one if so).
        try:
            self._lf.create_dataset(
                name=dataset_name,
                description=description,
            )
        except Exception:
            logger.warning(
                "DatasetCurator: failed to ensure dataset %r",
                dataset_name,
                exc_info=True,
            )
            return 0

        # 2. List recent traces (3x to leave room for filtering).
        traces = await self.list_traces(limit=max_items * 3)
        if not traces:
            logger.info("DatasetCurator: no traces found for curation")
            return 0

        # 3. Filter by enabled criteria (OR semantics; no filters = keep all).
        selected: list[tuple[TraceSummary, str]] = []
        for trace in traces:
            reason = _select_reason(
                trace,
                filter_errors=filter_errors,
                filter_high_latency=filter_high_latency,
                latency_threshold_ms=latency_threshold_ms,
            )
            if reason is None:
                continue
            selected.append((trace, reason))
            if len(selected) >= max_items:
                break

        if not selected:
            logger.info(
                "DatasetCurator: no traces matched filters for dataset %r",
                dataset_name,
            )
            return 0

        # 4-5. Fetch I/O and create dataset items (per-trace isolation).
        created = 0
        for trace, reason in selected:
            try:
                io = await self.fetch_trace_io(trace.trace_id)
                if io is None:
                    logger.warning(
                        "DatasetCurator: no root observation I/O for trace %s, skipping",
                        trace.trace_id,
                    )
                    continue
                self._lf.create_dataset_item(
                    dataset_name=dataset_name,
                    input=io["input"],
                    expected_output=io["output"],
                    source_trace_id=trace.trace_id,
                    metadata={
                        "session_id": trace.session_id,
                        "curated_from": reason,
                    },
                )
                created += 1
            except Exception:
                logger.warning(
                    "DatasetCurator: failed to create dataset item from trace %s",
                    trace.trace_id,
                    exc_info=True,
                )

        logger.info(
            "DatasetCurator: created %d/%d items in dataset %r",
            created,
            len(selected),
            dataset_name,
        )
        return created


def _find_root_observation(
    observations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Find the root observation (``parentObservationId`` is None).

    Prefer ``type=AGENT`` / ``name=invoke_agent`` when present, then any
    ``type=AGENT`` observation, then the first root.
    """
    roots = [
        obs
        for obs in observations
        if isinstance(obs, dict) and obs.get("parentObservationId") is None
    ]
    if not roots:
        return None
    for obs in roots:
        if obs.get("type") == "AGENT" and obs.get("name") == "invoke_agent":
            return obs
    for obs in roots:
        if obs.get("type") == "AGENT":
            return obs
    return roots[0]


def _select_reason(
    trace: TraceSummary,
    *,
    filter_errors: bool,
    filter_high_latency: bool,
    latency_threshold_ms: float,
) -> str | None:
    """Return the curation reason if the trace matches any enabled filter.

    Returns ``None`` when the trace should be skipped. When no filter is
    enabled, every trace matches as ``"manual"``.
    """
    if filter_errors and trace.has_error:
        return "error"
    if filter_high_latency and trace.latency_ms > latency_threshold_ms:
        return "high_latency"
    if not filter_errors and not filter_high_latency:
        return "manual"
    return None


__all__ = ["DatasetCurator", "TraceSummary"]
