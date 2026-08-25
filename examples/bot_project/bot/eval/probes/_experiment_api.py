"""Bounded Langfuse experiment compare API polling for B5 evidence."""

from __future__ import annotations

import base64
from typing import Final

import anyio
import httpx
from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.eval.probes._dispatch_models import ProbeDispatchError
from bot.eval.probes.evidence import ExperimentApiExcerpt

_POLL_ATTEMPTS: Final = 10
_POLL_SECONDS: Final = 1.0
_QUERY_TIMEOUT_SECONDS: Final = 5.0


class ExperimentQuery(BaseModel):
    """Credentials and run identity for one compare API lookup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    public_key: str
    secret_key: str
    run_name: str


class _ExperimentRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    experiment_id: str = Field(alias="id")
    name: str
    dataset_id: str | None = Field(default=None, alias="datasetId")
    item_count: int = Field(default=0, alias="itemCount")
    start_time: str | None = Field(default=None, alias="startTime")
    end_time: str | None = Field(default=None, alias="endTime")


class _ExperimentPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    data: list[_ExperimentRead]


def _fetch_experiment_dump(query: ExperimentQuery) -> list[ExperimentApiExcerpt]:
    credentials = base64.b64encode(f"{query.public_key}:{query.secret_key}".encode()).decode(
        "ascii"
    )
    with httpx.Client(timeout=_QUERY_TIMEOUT_SECONDS) as client:
        response = client.get(
            f"{query.host.rstrip('/')}/api/public/experiments",
            params={
                "fromStartTime": "2000-01-01T00:00:00Z",
                "toStartTime": "2100-01-01T00:00:00Z",
                "limit": 100,
            },
            headers={"Authorization": f"Basic {credentials}"},
        )
        response.raise_for_status()
        page = _ExperimentPage.model_validate(response.json())
    return [
        ExperimentApiExcerpt(
            experiment_id=item.experiment_id,
            name=item.name,
            dataset_id=item.dataset_id,
            item_count=item.item_count,
            start_time=item.start_time,
            end_time=item.end_time,
        )
        for item in page.data
        if item.name == query.run_name
    ]


async def poll_experiment_dump(query: ExperimentQuery) -> list[ExperimentApiExcerpt]:
    """Poll the compare API for a bounded ten-second visibility window."""
    latest: list[ExperimentApiExcerpt] = []
    for attempt in range(_POLL_ATTEMPTS):
        try:
            latest = await to_thread.run_sync(
                _fetch_experiment_dump,
                query,
                abandon_on_cancel=True,
            )
        except (httpx.HTTPError, ValidationError) as exc:
            if attempt + 1 == _POLL_ATTEMPTS:
                raise ProbeDispatchError(f"experiment compare query failed: {exc}") from exc
        if latest:
            return latest
        if attempt + 1 < _POLL_ATTEMPTS:
            await anyio.sleep(_POLL_SECONDS)
    return latest


__all__ = ["ExperimentQuery", "poll_experiment_dump"]
