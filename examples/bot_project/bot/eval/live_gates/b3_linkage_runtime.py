"""Bounded live service operations for the B3 experiment-linkage gate."""

from __future__ import annotations

import base64
import os
import socket
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

import anyio
import httpx
from anyio import to_thread
from langfuse import Langfuse
from langfuse.api.core.api_error import ApiError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.eval.evalenv import (
    PUBLIC_KEY_ENV,
    SECRET_KEY_ENV,
    LangfuseCredentials,
)
from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.trace.experiment_attrs import stable_experiment_id
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.store import SpanModel

HEALTH_TIMEOUT_SECONDS: Final = 2.0
SDK_TIMEOUT_SECONDS: Final = 8
EMIT_TIMEOUT_SECONDS: Final = 5.0
QUERY_TIMEOUT_SECONDS: Final = 3.0
POLL_TIMEOUT_SECONDS: Final = 60.0
POLL_BACKOFF_SECONDS: Final = (0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 10.0, 10.0, 8.0)
BACKFILL_POLL_INTERVAL_SECONDS: Final = 30.0
BACKFILL_TIMEOUT_SECONDS: Final = 390.0
DATASET_NAME: Final = "b3-linkage-probe"

_BACKFILL_TIMEOUT_DETAIL: Final = (
    "experiment found (attrs→traces→API chain OK) but dataset_run_item backfill "
    "did not materialize within 390s — Langfuse backfill is 5-min throttled; "
    "re-dispatch the gate or check worker logs"
)


class GateError(RuntimeError):
    def __init__(self, *, step: str, detail: str) -> None:
        self.step = step
        self.detail = detail
        super().__init__(f"{step} failed: {detail}")


class PreflightEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    langfuse_health: bool
    collector_port: bool
    missing: list[str]


class DatasetProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_name: str
    dataset_id: str
    item_id: str


class LinkageLookup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_found: bool
    linkage_signal: str | None


class ExperimentQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    public_key: str
    secret_key: str
    experiment_name: str
    dataset_id: str
    from_start_time: datetime
    to_start_time: datetime


class _ExperimentRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    name: str
    dataset_id: str | None = Field(default=None, alias="datasetId")
    item_count: int = Field(default=0, alias="itemCount")


class _ExperimentsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    data: list[_ExperimentRead]


def _probe_langfuse_health(host: str) -> bool:
    try:
        with httpx.Client(timeout=HEALTH_TIMEOUT_SECONDS) as client:
            response = client.get(f"{host.rstrip('/')}/api/public/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _probe_collector(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    if parsed.hostname is None:
        return False
    try:
        with socket.create_connection(
            (parsed.hostname, parsed.port or 4318),
            timeout=HEALTH_TIMEOUT_SECONDS,
        ):
            return True
    except OSError:
        return False


def run_preflight() -> PreflightEvidence:
    required_env = ("LANGFUSE_BASIC_AUTH",)
    missing = [name for name in required_env if not os.environ.get(name)]
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        missing.extend(
            name
            for name in (PUBLIC_KEY_ENV, SECRET_KEY_ENV)
            if not os.environ.get(name)
        )
    host = (
        credentials.host
        if credentials is not None and credentials.host is not None
        else os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    )
    endpoint = os.environ.get("OTEL_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
    health_ok = _probe_langfuse_health(host)
    collector_ok = _probe_collector(endpoint)
    if not health_ok:
        missing.append("langfuse:/api/public/health")
    if not collector_ok:
        missing.append("collector:4318")
    return PreflightEvidence(
        langfuse_health=health_ok,
        collector_port=collector_ok,
        missing=missing,
    )


def _create_probe_dataset_sync() -> DatasetProbe:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    client = Langfuse(
        base_url=host,
        public_key=credentials.public_key,
        secret_key=credentials.secret_key,
        timeout=SDK_TIMEOUT_SECONDS,
        tracing_enabled=False,
    )
    try:
        dataset = client.create_dataset(
            name=DATASET_NAME,
            description="Idempotent B3 experiment-linkage live probe.",
        )
        item = client.create_dataset_item(
            dataset_name=DATASET_NAME,
            input={"probe": "b3-linkage-smoke-v1"},
            expected_output={"linked": True},
        )
        return DatasetProbe(
            dataset_name=DATASET_NAME,
            dataset_id=dataset.id,
            item_id=item.id,
        )
    finally:
        client.shutdown()


async def create_probe_dataset() -> DatasetProbe:
    try:
        with anyio.fail_after(SDK_TIMEOUT_SECONDS + 2):
            return await to_thread.run_sync(
                _create_probe_dataset_sync,
                abandon_on_cancel=True,
            )
    except TimeoutError as exc:
        raise GateError(step="dataset_create", detail="SDK call exceeded 10 seconds") from exc
    except (ApiError, httpx.HTTPError) as exc:
        raise GateError(step="dataset_create", detail=str(exc)) from exc


async def mint_experiment_id(dataset: DatasetProbe, run_name: str) -> str:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"

    def mint() -> str:
        return stable_experiment_id(
            host=host,
            public_key=credentials.public_key,
            secret_key=credentials.secret_key,
            dataset_id=dataset.dataset_id,
            item_id=dataset.item_id,
            run_name=run_name,
        )

    try:
        with anyio.fail_after(12.0):
            return await to_thread.run_sync(mint, abandon_on_cancel=True)
    except TimeoutError as exc:
        raise GateError(
            step="stable_experiment_id",
            detail="ID minting exceeded 12 seconds",
        ) from exc


async def emit_probe_span(span: SpanModel) -> None:
    endpoint = os.environ.get("OTEL_TRACES_ENDPOINT", "http://localhost:4318/v1/traces")
    headers = {
        "Authorization": f"Basic {os.environ['LANGFUSE_BASIC_AUTH']}",
        "x-langfuse-ingestion-version": "4",
    }
    with tempfile.TemporaryDirectory(prefix="modex-b3-linkage-") as raw_dir:
        store = OtelSpanTraceStore(
            base_dir=Path(raw_dir),
            backend=TraceBackend.OTEL_HTTP,
            otlp_endpoint=endpoint,
            otlp_headers=headers,
            otlp_service_name="modex_agent.eval.b3",
            export_queue_size=1,
        )
        deadline = time.monotonic() + EMIT_TIMEOUT_SECONDS
        try:
            await store.save_span(span)
            while store.exported_spans == 0 and store.dropped_spans == 0:
                if time.monotonic() >= deadline:
                    raise GateError(
                        step="span_emit",
                        detail=f"collector did not acknowledge trace {span.trace_id} within 5 seconds",
                    )
                await anyio.sleep(0.05)
            if store.dropped_spans:
                raise GateError(
                    step="span_emit",
                    detail=f"collector dropped trace {span.trace_id}",
                )
        finally:
            await to_thread.run_sync(store.close)


def _fetch_linkage(query: ExperimentQuery) -> LinkageLookup:
    credentials = base64.b64encode(f"{query.public_key}:{query.secret_key}".encode()).decode(
        "ascii"
    )
    params: dict[str, str | int] = {
        "fromStartTime": query.from_start_time.isoformat().replace("+00:00", "Z"),
        "toStartTime": query.to_start_time.isoformat().replace("+00:00", "Z"),
        "limit": 100,
    }
    try:
        with httpx.Client(timeout=QUERY_TIMEOUT_SECONDS) as client:
            response = client.get(
                f"{query.host.rstrip('/')}/api/public/experiments",
                params=params,
                headers={"Authorization": f"Basic {credentials}"},
            )
            response.raise_for_status()
            experiments = _ExperimentsResponse.model_validate(response.json()).data
    except (httpx.HTTPError, ValidationError) as exc:
        raise GateError(step="experiment_query", detail=str(exc)) from exc

    for experiment in experiments:
        if experiment.name != query.experiment_name:
            continue
        if experiment.dataset_id not in (None, query.dataset_id):
            continue
        signal = (
            f"experiments.itemCount={experiment.item_count}" if experiment.item_count > 0 else None
        )
        return LinkageLookup(experiment_found=True, linkage_signal=signal)
    return LinkageLookup(experiment_found=False, linkage_signal=None)


async def poll_linkage(
    query: ExperimentQuery,
    *,
    backoff_seconds: tuple[float, ...] = POLL_BACKOFF_SECONDS,
) -> LinkageLookup:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    latest = await to_thread.run_sync(_fetch_linkage, query, abandon_on_cancel=True)
    if latest.experiment_found:
        if latest.linkage_signal is not None:
            return latest
    else:
        for delay in backoff_seconds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return latest
            await anyio.sleep(min(delay, remaining))
            with anyio.move_on_after(max(deadline - time.monotonic(), 0.0)) as scope:
                latest = await to_thread.run_sync(
                    _fetch_linkage,
                    query,
                    abandon_on_cancel=True,
                )
            if scope.cancel_called:
                return latest
            if latest.experiment_found:
                break
        if not latest.experiment_found:
            return latest
        if latest.linkage_signal is not None:
            return latest

    backfill_deadline = time.monotonic() + BACKFILL_TIMEOUT_SECONDS
    max_attempts = int(BACKFILL_TIMEOUT_SECONDS / BACKFILL_POLL_INTERVAL_SECONDS)
    for attempt in range(1, max_attempts + 1):
        print(f"waiting for experiment backfill (attempt {attempt}/{max_attempts}, itemCount=0)")
        remaining = backfill_deadline - time.monotonic()
        if remaining <= 0:
            break
        await anyio.sleep(min(BACKFILL_POLL_INTERVAL_SECONDS, remaining))
        with anyio.move_on_after(max(backfill_deadline - time.monotonic(), 0.0)) as scope:
            latest = await to_thread.run_sync(
                _fetch_linkage,
                query,
                abandon_on_cancel=True,
            )
        if scope.cancel_called:
            break
        if latest.linkage_signal is not None:
            return latest
    raise TimeoutError(_BACKFILL_TIMEOUT_DETAIL)
