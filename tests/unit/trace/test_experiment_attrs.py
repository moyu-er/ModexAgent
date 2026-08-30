from __future__ import annotations

import base64
import copy
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from modex_agent.trace import (
    ExperimentAttribute,
    ExperimentLinkage,
    ExperimentLinkageError,
    attach_experiment_attrs,
    stable_experiment_id,
)
from modex_agent.trace.store import SpanModel


def _span(attributes: dict[str, Any]) -> SpanModel:
    return SpanModel(
        trace_id="trace-1",
        span_id="span-1",
        name="invoke_agent",
        start_time=1.0,
        attributes=attributes,
    )


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client = httpx.Client

    def build_client(**kwargs: Any) -> httpx.Client:
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", build_client)


def test_experiment_linkage_rejects_missing_and_extra_fields() -> None:
    # Given
    valid = {
        "experiment_id": "experiment-1",
        "experiment_name": "run-1",
        "dataset_id": "dataset-1",
        "item_id": "item-1",
    }

    # When / Then
    with pytest.raises(ValidationError):
        ExperimentLinkage.model_validate(
            {key: value for key, value in valid.items() if key != "item_id"}
        )
    with pytest.raises(ValidationError):
        ExperimentLinkage.model_validate({**valid, "unexpected": "forbidden"})


def test_attach_experiment_attrs_copies_exact_linkage_and_preserves_original() -> None:
    # Given
    span = _span({"existing": {"nested": [1, 2]}, "count": 3})
    original_attributes = copy.deepcopy(span.attributes)
    linkage = ExperimentLinkage(
        experiment_id="experiment-1",
        experiment_name="run-1",
        dataset_id="dataset-1",
        item_id="item-1",
    )
    expected_linkage = {
        "langfuse.experiment.id": "experiment-1",
        "langfuse.experiment.name": "run-1",
        "langfuse.experiment.dataset.id": "dataset-1",
        "langfuse.experiment.item.id": "item-1",
        "langfuse.experiment.item.root_observation_id": "span-1",
    }

    # When
    linked_span = attach_experiment_attrs(span, linkage)

    # Then
    assert linked_span is not span
    assert span.attributes == original_attributes
    assert linked_span.attributes == {**original_attributes, **expected_linkage}
    assert {
        key.value: linked_span.attributes[key.value] for key in ExperimentAttribute
    } == expected_linkage


def test_attach_experiment_attrs_honors_explicit_root_span_id() -> None:
    # Given
    span = _span({})
    linkage = ExperimentLinkage(
        experiment_id="experiment-1",
        experiment_name="run-1",
        dataset_id="dataset-1",
        item_id="item-1",
    )

    # When
    linked_span = attach_experiment_attrs(
        span,
        linkage,
        root_span_id="root-span-override",
    )

    # Then
    assert (
        linked_span.attributes[ExperimentAttribute.ITEM_ROOT_OBSERVATION_ID.value]
        == "root-span-override"
    )


def test_attach_experiment_attrs_overwrites_existing_linkage_value() -> None:
    # Given
    span = _span({ExperimentAttribute.ID.value: "stale", "existing": "kept"})
    linkage = ExperimentLinkage(
        experiment_id="fresh",
        experiment_name="run-1",
        dataset_id="dataset-1",
        item_id="item-1",
    )

    # When
    linked_span = attach_experiment_attrs(span, linkage)

    # Then
    assert linked_span.attributes[ExperimentAttribute.ID.value] == "fresh"
    assert linked_span.attributes["existing"] == "kept"
    assert span.attributes[ExperimentAttribute.ID.value] == "stale"


def test_stable_experiment_id_is_idempotent_and_matches_sdk_request_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    requests: list[dict[str, Any]] = []
    expected_auth = base64.b64encode(b"public:secret").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/public/dataset-run-items"
        assert request.headers["Authorization"] == f"Basic {expected_auth}"
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"id": "run-item-1", "datasetRunId": "experiment-stable"},
        )

    _install_transport(monkeypatch, handler)

    # When
    first = stable_experiment_id(
        host="http://langfuse/",
        public_key="public",
        secret_key="secret",
        dataset_id="dataset-1",
        item_id="item-1",
        run_name="run-1",
    )
    second = stable_experiment_id(
        host="http://langfuse/",
        public_key="public",
        secret_key="secret",
        dataset_id="dataset-1",
        item_id="item-1",
        run_name="run-1",
    )

    # Then
    assert first == second == "experiment-stable"
    assert requests[0] == requests[1]
    assert set(requests[0]) == {"runName", "datasetItemId", "traceId"}
    assert requests[0]["runName"] == "run-1"
    assert requests[0]["datasetItemId"] == "item-1"
    assert isinstance(requests[0]["traceId"], str)
    assert requests[0]["traceId"]


def test_stable_experiment_id_wraps_http_error_with_host_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="conflict")

    _install_transport(monkeypatch, handler)

    # When
    with pytest.raises(ExperimentLinkageError) as raised:
        stable_experiment_id(
            host="http://langfuse/",
            public_key="public",
            secret_key="secret",
            dataset_id="dataset-1",
            item_id="item-1",
            run_name="run-1",
        )

    # Then
    assert raised.value.host == "http://langfuse"
    assert raised.value.status_code == 409
    assert raised.value.detail == "conflict"
    assert "http://langfuse" in str(raised.value)
    assert "409" in str(raised.value)


def test_stable_experiment_id_retries_502_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    attempts: list[int] = []
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(
            200,
            json={"id": "run-item-1", "datasetRunId": "experiment-stable"},
        )

    _install_transport(monkeypatch, handler)

    # When
    result = stable_experiment_id(
        host="http://langfuse/",
        public_key="public",
        secret_key="secret",
        dataset_id="dataset-1",
        item_id="item-1",
        run_name="run-1",
    )

    # Then
    assert result == "experiment-stable"
    assert len(attempts) == 2
    assert delays == [1.0]


def test_stable_experiment_id_exhausts_retries_then_raises_last_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    attempts: list[int] = []
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(502, text="bad gateway")

    _install_transport(monkeypatch, handler)

    # When
    with pytest.raises(ExperimentLinkageError) as raised:
        stable_experiment_id(
            host="http://langfuse/",
            public_key="public",
            secret_key="secret",
            dataset_id="dataset-1",
            item_id="item-1",
            run_name="run-1",
        )

    # Then
    assert raised.value.status_code == 502
    assert raised.value.detail == "bad gateway"
    assert len(attempts) == 4
    assert delays == [1.0, 2.0, 4.0]


def test_stable_experiment_id_raises_non_retryable_status_after_one_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    attempts: list[int] = []
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, text="unauthorized")

    _install_transport(monkeypatch, handler)

    # When
    with pytest.raises(ExperimentLinkageError) as raised:
        stable_experiment_id(
            host="http://langfuse/",
            public_key="public",
            secret_key="secret",
            dataset_id="dataset-1",
            item_id="item-1",
            run_name="run-1",
        )

    # Then
    assert raised.value.status_code == 401
    assert raised.value.detail == "unauthorized"
    assert len(attempts) == 1
    assert delays == []


def test_stable_experiment_id_retries_request_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    attempts: list[int] = []
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(
            200,
            json={"id": "run-item-1", "datasetRunId": "experiment-stable"},
        )

    _install_transport(monkeypatch, handler)

    # When
    result = stable_experiment_id(
        host="http://langfuse/",
        public_key="public",
        secret_key="secret",
        dataset_id="dataset-1",
        item_id="item-1",
        run_name="run-1",
    )

    # Then
    assert result == "experiment-stable"
    assert len(attempts) == 2
    assert delays == [1.0]


def test_stable_experiment_id_wraps_network_error_with_host_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    delays: list[float] = []
    monkeypatch.setattr(time, "sleep", delays.append)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    _install_transport(monkeypatch, handler)

    # When
    with pytest.raises(ExperimentLinkageError) as raised:
        stable_experiment_id(
            host="http://langfuse",
            public_key="public",
            secret_key="secret",
            dataset_id="dataset-1",
            item_id="item-1",
            run_name="run-1",
        )

    # Then
    assert raised.value.host == "http://langfuse"
    assert raised.value.status_code is None
    assert raised.value.detail == "offline"
    assert "http://langfuse" in str(raised.value)
    assert "network" in str(raised.value)
