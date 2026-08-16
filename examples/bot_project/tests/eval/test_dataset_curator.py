from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import MagicMock

import bot.eval.dataset_curator as dataset_curator_module
import httpx
import pytest
from bot.eval.dataset_curator import (
    DatasetCurator,
    TraceSummary,
    _find_root_observation,
)
from langfuse import Langfuse


def _observation(
    *,
    observation_id: str,
    trace_id: str,
    parent_observation_id: str | None = None,
    name: str = "",
    level: str = "DEFAULT",
    latency: float | None = 0.25,
    session_id: str | None = "session-1",
    observation_type: str = "AGENT",
) -> dict[str, str | float | None]:
    return {
        "id": observation_id,
        "traceId": trace_id,
        "parentObservationId": parent_observation_id,
        "type": observation_type,
        "level": level,
        "latency": latency,
        "sessionId": session_id,
        "name": name,
    }


def _curator_with_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[DatasetCurator, list[httpx.Request]]:
    requests: list[httpx.Request] = []
    async_client = httpx.AsyncClient

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def client_factory(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return async_client(transport=httpx.MockTransport(recording_handler), timeout=timeout)

    monkeypatch.setattr(dataset_curator_module.httpx, "AsyncClient", client_factory)
    curator = DatasetCurator(
        langfuse_host="https://langfuse.test/",
        public_key="pk-test-fake",
        secret_key="sk-test-fake",
        langfuse_client=MagicMock(spec=Langfuse),
    )
    return curator, requests


async def test_list_traces_parses_real_shaped_root_agent_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            _observation(
                observation_id="obs-root",
                trace_id="trace-1",
                name="",
                level="ERROR",
                latency=1.75,
                session_id="session-live",
            )
        ],
        "meta": {"page": 1, "limit": 50, "totalItems": 1, "totalPages": 1},
    }
    curator, requests = _curator_with_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=payload),
    )

    summaries = await curator.list_traces(limit=7)

    assert summaries == [
        TraceSummary(
            trace_id="trace-1",
            session_id="session-live",
            name="invoke_agent",
            observation_count=0,
            has_error=True,
            latency_ms=1.75,
        )
    ]
    assert requests[0].url.path == "/api/public/v2/observations"
    assert requests[0].url.params["fields"] == "core,basic,io"
    assert requests[0].url.params["limit"] == "7"
    assert requests[0].url.params["orderBy"] == "timestamp_desc"
    assert json.loads(requests[0].url.params["filter"]) == [
        {"type": "string", "column": "type", "operator": "=", "value": "AGENT"}
    ]


async def test_list_traces_excludes_agent_observation_with_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            _observation(observation_id="obs-root", trace_id="trace-1"),
            _observation(
                observation_id="obs-subagent",
                trace_id="trace-1",
                parent_observation_id="obs-handoff",
            ),
        ]
    }
    curator, _ = _curator_with_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=payload),
    )

    summaries = await curator.list_traces()

    assert [summary.trace_id for summary in summaries] == ["trace-1"]


async def test_list_traces_adds_session_id_to_observation_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    curator, requests = _curator_with_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": []}),
    )

    await curator.list_traces(session_id="session-filter")

    assert json.loads(requests[0].url.params["filter"]) == [
        {"type": "string", "column": "type", "operator": "=", "value": "AGENT"},
        {
            "type": "string",
            "column": "sessionId",
            "operator": "=",
            "value": "session-filter",
        },
    ]


async def test_list_traces_deduplicates_trace_id_in_response_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "data": [
            _observation(
                observation_id="obs-newest",
                trace_id="trace-shared",
                name="newest",
            ),
            _observation(
                observation_id="obs-older",
                trace_id="trace-shared",
                name="older",
            ),
        ]
    }
    curator, _ = _curator_with_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=payload),
    )

    summaries = await curator.list_traces()

    assert [summary.name for summary in summaries] == ["newest"]


@pytest.mark.parametrize(
    ("status_code", "content"),
    [(503, b"unavailable"), (200, b"not-json")],
)
async def test_list_traces_returns_empty_for_unusable_response(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    content: bytes,
) -> None:
    curator, _ = _curator_with_transport(
        monkeypatch,
        lambda request: httpx.Response(status_code, content=content),
    )

    summaries = await curator.list_traces()

    assert summaries == []


def test_find_root_observation_prefers_agent_root_with_empty_name() -> None:
    non_agent_root = _observation(
        observation_id="span-root",
        trace_id="trace-1",
        name="invoke_agent",
        observation_type="SPAN",
    )
    agent_root = _observation(
        observation_id="agent-root",
        trace_id="trace-1",
        name="",
    )
    named_agent_root = _observation(
        observation_id="named-agent-root",
        trace_id="trace-1",
        name="invoke_agent",
    )

    root = _find_root_observation([non_agent_root, agent_root, named_agent_root])

    assert root == agent_root
