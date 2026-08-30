from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from typer.testing import CliRunner

from modex_agent.trace.experiment_attrs import (
    ExperimentAttribute,
    ExperimentLinkageError,
)

type _ClientValue = str | int | bool
type _ItemValue = str | dict[str, str] | dict[str, bool]


def _ready_preflight():
    from bot.eval.live_gates.b3_linkage_runtime import PreflightEvidence

    return PreflightEvidence(
        langfuse_health=True,
        collector_port=True,
        missing=[],
    )


def _probe_dataset():
    from bot.eval.live_gates.b3_linkage_runtime import DatasetProbe

    return DatasetProbe(
        dataset_name="b3-linkage-probe",
        dataset_id="dataset-b3",
        item_id="item-b3",
    )


def _found_linkage():
    from bot.eval.live_gates.b3_linkage_runtime import LinkageLookup

    return LinkageLookup(
        experiment_found=True,
        linkage_signal="experiments.itemCount=1",
    )


async def test_run_gate_writes_green_evidence_and_exact_experiment_span_attrs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_experiment_linkage as gate

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    emitted_spans = []

    async def capture_span(span):
        emitted_spans.append(span)

    monkeypatch.setattr(gate, "_run_preflight", _ready_preflight)
    monkeypatch.setattr(gate, "_create_probe_dataset", AsyncMock(return_value=_probe_dataset()))
    monkeypatch.setattr(gate, "_mint_experiment_id", AsyncMock(return_value="experiment-b3"))
    monkeypatch.setattr(gate, "_emit_probe_span", capture_span)
    monkeypatch.setattr(gate, "_poll_linkage", AsyncMock(return_value=_found_linkage()))
    evidence_path = tmp_path / "b3_linkage.json"

    # When
    evidence = await gate.run_gate(evidence_path=evidence_path)

    # Then
    assert evidence.passed is True
    assert evidence.dataset_name == "b3-linkage-probe"
    assert evidence.dataset_id == "dataset-b3"
    assert evidence.item_id == "item-b3"
    assert evidence.experiment_id == "experiment-b3"
    assert evidence.experiment_name == "b3-linkage-smoke-v1"
    assert evidence.experiment_found is True
    assert evidence.linkage_signal == "experiments.itemCount=1"
    assert evidence.error is None
    assert len(emitted_spans) == 1
    span = emitted_spans[0]
    assert span.trace_id == evidence.span_trace_id
    assert span.name == "b3.linkage.probe"
    assert span.parent_span_id is None
    assert span.attributes == {
        ExperimentAttribute.ID.value: "experiment-b3",
        ExperimentAttribute.NAME.value: "b3-linkage-smoke-v1",
        ExperimentAttribute.DATASET_ID.value: "dataset-b3",
        ExperimentAttribute.ITEM_ID.value: "item-b3",
        ExperimentAttribute.ITEM_ROOT_OBSERVATION_ID.value: span.span_id,
    }
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == evidence.model_dump(mode="json")


async def test_run_gate_stops_after_preflight_failure_and_writes_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_experiment_linkage as gate
    from bot.eval.live_gates.b3_linkage_runtime import PreflightEvidence

    create_dataset = AsyncMock()
    monkeypatch.setattr(
        gate,
        "_run_preflight",
        lambda: PreflightEvidence(
            langfuse_health=True,
            collector_port=False,
            missing=["collector:4318"],
        ),
    )
    monkeypatch.setattr(gate, "_create_probe_dataset", create_dataset)
    evidence_path = tmp_path / "b3_linkage.json"

    # When
    evidence = await gate.run_gate(evidence_path=evidence_path)

    # Then
    assert evidence.passed is False
    assert evidence.error == "preflight failed: collector:4318"
    assert evidence.span_trace_id is None
    assert evidence_path.is_file()
    create_dataset.assert_not_awaited()


async def test_run_gate_reports_emitted_span_when_experiment_is_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_experiment_linkage as gate
    from bot.eval.live_gates.b3_linkage_runtime import LinkageLookup

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setattr(gate, "_run_preflight", _ready_preflight)
    monkeypatch.setattr(gate, "_create_probe_dataset", AsyncMock(return_value=_probe_dataset()))
    monkeypatch.setattr(gate, "_mint_experiment_id", AsyncMock(return_value="experiment-b3"))
    monkeypatch.setattr(gate, "_emit_probe_span", AsyncMock())
    monkeypatch.setattr(
        gate,
        "_poll_linkage",
        AsyncMock(return_value=LinkageLookup(experiment_found=False, linkage_signal=None)),
    )

    # When
    evidence = await gate.run_gate(evidence_path=tmp_path / "b3_linkage.json")

    # Then
    assert evidence.passed is False
    assert evidence.experiment_found is False
    assert evidence.span_trace_id is not None
    assert evidence.span_trace_id in (evidence.error or "")
    assert "experiment_poll" in (evidence.error or "")


async def test_run_gate_backfill_timeout_evidence_keeps_experiment_and_trace_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_experiment_linkage as gate

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    detail = (
        "experiment found (attrs→traces→API chain OK) but dataset_run_item "
        "backfill did not materialize within 390s — Langfuse backfill is 5-min "
        "throttled; re-dispatch the gate or check worker logs"
    )
    monkeypatch.setattr(gate, "_run_preflight", _ready_preflight)
    monkeypatch.setattr(gate, "_create_probe_dataset", AsyncMock(return_value=_probe_dataset()))
    monkeypatch.setattr(gate, "_mint_experiment_id", AsyncMock(return_value="experiment-b3"))
    monkeypatch.setattr(gate, "_emit_probe_span", AsyncMock())
    monkeypatch.setattr(gate, "_poll_linkage", AsyncMock(side_effect=TimeoutError(detail)))

    # When
    evidence = await gate.run_gate(evidence_path=tmp_path / "b3_linkage.json")

    # Then
    assert evidence.passed is False
    assert evidence.experiment_id == "experiment-b3"
    assert evidence.span_trace_id is not None
    assert evidence.error == detail


async def test_run_gate_bounds_stable_id_failure_with_named_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_experiment_linkage as gate

    error = ExperimentLinkageError(
        host="http://localhost:3000",
        status_code=409,
        detail="malformed linkage",
    )
    monkeypatch.setattr(gate, "_run_preflight", _ready_preflight)
    monkeypatch.setattr(gate, "_create_probe_dataset", AsyncMock(return_value=_probe_dataset()))
    monkeypatch.setattr(gate, "_mint_experiment_id", AsyncMock(side_effect=error))

    # When
    evidence = await gate.run_gate(evidence_path=tmp_path / "b3_linkage.json")

    # Then
    assert evidence.passed is False
    assert evidence.dataset_id == "dataset-b3"
    assert evidence.span_trace_id is None
    assert "stable_experiment_id" in (evidence.error or "")
    assert "HTTP 409" in (evidence.error or "")


async def test_poll_linkage_never_found_exhausts_finite_backoff_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_linkage_runtime as runtime

    fetch = Mock(return_value=runtime.LinkageLookup(experiment_found=False, linkage_signal=None))
    sleep = AsyncMock()
    monkeypatch.setattr(runtime, "_fetch_linkage", fetch)
    monkeypatch.setattr(runtime.anyio, "sleep", sleep)
    query = runtime.ExperimentQuery(
        host="http://localhost:3000",
        public_key="public",
        secret_key="secret",
        experiment_name="b3-linkage-smoke-v1",
        dataset_id="dataset-b3",
        from_start_time=datetime.now(UTC),
        to_start_time=datetime.now(UTC),
    )

    # When
    result = await runtime.poll_linkage(query, backoff_seconds=(0.0, 0.0, 0.0))

    # Then
    assert result.experiment_found is False
    assert fetch.call_count == 4
    assert sleep.await_count == 3
    assert sum(runtime.POLL_BACKOFF_SECONDS) <= 60.0


async def test_poll_linkage_waits_for_delayed_backfill_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    from bot.eval.live_gates import b3_linkage_runtime as runtime

    pending = runtime.LinkageLookup(experiment_found=True, linkage_signal=None)
    linked = runtime.LinkageLookup(
        experiment_found=True,
        linkage_signal="experiments.itemCount=1",
    )
    fetch = Mock(side_effect=(pending, pending, linked))
    sleep = AsyncMock()
    monkeypatch.setattr(runtime, "_fetch_linkage", fetch)
    monkeypatch.setattr(runtime.anyio, "sleep", sleep)
    query = runtime.ExperimentQuery(
        host="http://localhost:3000",
        public_key="public",
        secret_key="secret",
        experiment_name="b3-linkage-smoke-v1",
        dataset_id="dataset-b3",
        from_start_time=datetime.now(UTC),
        to_start_time=datetime.now(UTC),
    )

    # When
    result = await runtime.poll_linkage(query)

    # Then
    assert result == linked
    assert fetch.call_count == 3
    assert sleep.await_count == 2
    assert capsys.readouterr().out.splitlines() == [
        "waiting for experiment backfill (attempt 1/13, itemCount=0)",
        "waiting for experiment backfill (attempt 2/13, itemCount=0)",
    ]


async def test_poll_linkage_backfill_timeout_is_bounded_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    from bot.eval.live_gates import b3_linkage_runtime as runtime

    pending = runtime.LinkageLookup(experiment_found=True, linkage_signal=None)
    fetch = Mock(return_value=pending)
    sleep = AsyncMock()
    monkeypatch.setattr(runtime, "_fetch_linkage", fetch)
    monkeypatch.setattr(runtime.anyio, "sleep", sleep)
    query = runtime.ExperimentQuery(
        host="http://localhost:3000",
        public_key="public",
        secret_key="secret",
        experiment_name="b3-linkage-smoke-v1",
        dataset_id="dataset-b3",
        from_start_time=datetime.now(UTC),
        to_start_time=datetime.now(UTC),
    )

    # When
    with pytest.raises(TimeoutError) as exc_info:
        await runtime.poll_linkage(query)

    # Then
    assert str(exc_info.value) == (
        "experiment found (attrs→traces→API chain OK) but dataset_run_item "
        "backfill did not materialize within 390s — Langfuse backfill is 5-min "
        "throttled; re-dispatch the gate or check worker logs"
    )
    assert runtime.BACKFILL_POLL_INTERVAL_SECONDS == 30.0
    assert runtime.BACKFILL_TIMEOUT_SECONDS == 390.0
    assert fetch.call_count == 14
    assert sleep.await_count == 13
    assert {await_call.args for await_call in sleep.await_args_list} == {(30.0,)}
    assert capsys.readouterr().out.splitlines()[-1] == (
        "waiting for experiment backfill (attempt 13/13, itemCount=0)"
    )


async def test_mint_experiment_id_uses_dataset_probe_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_linkage_runtime as runtime

    captured: dict[str, str] = {}

    def fake_stable_experiment_id(**kwargs: str) -> str:
        captured.update(kwargs)
        return "experiment-b3"

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setattr(runtime, "stable_experiment_id", fake_stable_experiment_id)

    # When
    experiment_id = await runtime.mint_experiment_id(
        _probe_dataset(),
        "b3-linkage-smoke-v1",
    )

    # Then
    assert experiment_id == "experiment-b3"
    assert captured["dataset_id"] == "dataset-b3"
    assert captured["item_id"] == "item-b3"


def test_create_probe_dataset_uses_server_minted_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_linkage_runtime as runtime

    create_item_kwargs: dict[str, _ItemValue] = {}

    class FakeLangfuse:
        def __init__(self, **_kwargs: _ClientValue) -> None:
            pass

        def create_dataset(self, **_kwargs: str) -> SimpleNamespace:
            return SimpleNamespace(id="dataset-b3")

        def create_dataset_item(self, **kwargs: _ItemValue) -> SimpleNamespace:
            create_item_kwargs.update(kwargs)
            return SimpleNamespace(id="cms-server-item")

        def shutdown(self) -> None:
            pass

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setattr(runtime, "Langfuse", FakeLangfuse)

    # When
    probe = runtime._create_probe_dataset_sync()

    # Then
    assert probe.item_id == "cms-server-item"
    assert "id" not in create_item_kwargs


@pytest.mark.parametrize(("passed", "exit_code"), [(True, 0), (False, 1)])
def test_cli_exit_code_follows_gate_evidence(
    passed: bool,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from bot.eval.live_gates import b3_experiment_linkage as gate

    evidence = gate.B3ExperimentLinkageEvidence(
        passed=passed,
        checked_at=datetime.now(UTC),
        preflight=_ready_preflight(),
        experiment_name="b3-linkage-smoke-v1",
        experiment_found=passed,
        linkage_signal="experiments.itemCount=1" if passed else None,
        error=None if passed else "preflight failed",
    )
    monkeypatch.setattr(gate.anyio, "run", lambda _func: evidence)

    # When
    result = CliRunner().invoke(gate.app)

    # Then
    assert result.exit_code == exit_code
