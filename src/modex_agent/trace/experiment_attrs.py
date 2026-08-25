from __future__ import annotations

import base64
import hashlib
import time
from enum import StrEnum
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from modex_agent.trace.store import SpanModel

_TIMEOUT_SECONDS: Final = 10.0
_DATASET_RUN_ITEMS_PATH: Final = "/api/public/dataset-run-items"
_LINKAGE_RETRIES: Final = 3
_LINKAGE_BACKOFF_SECONDS: Final = 1.0
_RETRYABLE_STATUSES: Final = frozenset({429, 502, 503, 504})


class ExperimentAttribute(StrEnum):
    ID = "langfuse.experiment.id"
    NAME = "langfuse.experiment.name"
    DATASET_ID = "langfuse.experiment.dataset.id"
    ITEM_ID = "langfuse.experiment.item.id"
    ITEM_ROOT_OBSERVATION_ID = "langfuse.experiment.item.root_observation_id"


class ExperimentLinkage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    experiment_name: str
    dataset_id: str
    item_id: str


class ExperimentLinkageError(Exception):
    def __init__(self, *, host: str, status_code: int | None, detail: str) -> None:
        self.host = host
        self.status_code = status_code
        self.detail = detail
        status = "network error" if status_code is None else f"HTTP {status_code}"
        super().__init__(f"Langfuse experiment linkage failed at {host}: {status}: {detail}")


class _CreateDatasetRunItemRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    run_name: str = Field(alias="runName")
    dataset_item_id: str = Field(alias="datasetItemId")
    trace_id: str = Field(alias="traceId")


class _CreateDatasetRunItemResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    id: str
    dataset_run_id: str = Field(alias="datasetRunId")


def attach_experiment_attrs(
    span: SpanModel,
    linkage: ExperimentLinkage,
    *,
    root_span_id: str | None = None,
) -> SpanModel:
    """Attach all five Langfuse experiment attributes to a copied span.

    The item root observation ID defaults to the carrying span's own ID. This
    fifth attribute enables Langfuse experiment item materialization.
    """
    experiment_attributes = {
        ExperimentAttribute.ID.value: linkage.experiment_id,
        ExperimentAttribute.NAME.value: linkage.experiment_name,
        ExperimentAttribute.DATASET_ID.value: linkage.dataset_id,
        ExperimentAttribute.ITEM_ID.value: linkage.item_id,
        ExperimentAttribute.ITEM_ROOT_OBSERVATION_ID.value: (
            span.span_id if root_span_id is None else root_span_id
        ),
    }
    return span.model_copy(
        update={"attributes": {**span.attributes, **experiment_attributes}},
    )


def stable_experiment_id(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    dataset_id: str,
    item_id: str,
    run_name: str,
) -> str:
    """Mint the stable experiment ID through Langfuse's events_only stub.

    This synchronous helper is intended for scripts. Live verification against
    Langfuse 4.11.0 on 2026-08-20 established that, in ``events_only`` mode,
    this POST validates that ``item_id`` exists and returns a stable
    ``datasetRunId`` without writing dataset-run rows. Experiment membership is
    created solely from the five ``langfuse.experiment.*`` OTel span attributes;
    the item root observation ID enables experiment item materialization.

    Transient failures (HTTP 429/502/503/504 or network errors) are retried
    up to 3 times with exponentially doubling backoff; other failures raise
    ``ExperimentLinkageError`` on the first attempt.
    """
    normalized_host = host.rstrip("/")
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    trace_id = hashlib.sha256(f"{dataset_id}\0{run_name}".encode()).hexdigest()[:32]
    payload = _CreateDatasetRunItemRequest(
        runName=run_name,
        datasetItemId=item_id,
        traceId=trace_id,
    )
    status_code: int | None = None
    detail = ""
    delay = _LINKAGE_BACKOFF_SECONDS
    for attempt in range(_LINKAGE_RETRIES + 1):
        if attempt > 0:
            time.sleep(delay)
            delay *= 2
        try:
            with httpx.Client(
                headers={"Authorization": f"Basic {credentials}"},
                timeout=_TIMEOUT_SECONDS,
            ) as client:
                response = client.post(
                    f"{normalized_host}{_DATASET_RUN_ITEMS_PATH}",
                    json=payload.model_dump(mode="json", by_alias=True),
                )
        except httpx.RequestError as exc:
            status_code = None
            detail = str(exc)
        else:
            if response.is_success:
                return (
                    _CreateDatasetRunItemResponse.model_validate(response.json()).dataset_run_id
                )
            status_code = response.status_code
            detail = response.text[:500]
            if status_code not in _RETRYABLE_STATUSES:
                break
    raise ExperimentLinkageError(
        host=normalized_host,
        status_code=status_code,
        detail=detail,
    )
