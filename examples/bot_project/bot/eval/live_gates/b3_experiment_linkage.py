"""Manual B3 gate proving Langfuse experiment linkage at runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, assert_never
from uuid import uuid4

import anyio
import typer
from pydantic import BaseModel, ConfigDict

from bot.eval.evalenv import LangfuseCredentials
from bot.eval.live_gates.b3_linkage_runtime import (
    DatasetProbe,
    ExperimentQuery,
    GateError,
    LinkageLookup,
    PreflightEvidence,
)
from bot.eval.live_gates.b3_linkage_runtime import (
    create_probe_dataset as _create_probe_dataset,
)
from bot.eval.live_gates.b3_linkage_runtime import emit_probe_span as _emit_probe_span
from bot.eval.live_gates.b3_linkage_runtime import mint_experiment_id as _mint_experiment_id
from bot.eval.live_gates.b3_linkage_runtime import poll_linkage as _poll_linkage
from bot.eval.live_gates.b3_linkage_runtime import run_preflight as _run_preflight
from modex_agent.trace.experiment_attrs import (
    ExperimentLinkage,
    ExperimentLinkageError,
    attach_experiment_attrs,
)
from modex_agent.trace.store import SpanModel

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

_EVIDENCE_PATH: Final = Path("evals/evidence/b3_linkage.json")
_EXPERIMENT_NAME: Final = "b3-linkage-smoke-v1"
_WINDOW: Final = timedelta(minutes=5)


class B3ExperimentLinkageEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    checked_at: datetime
    preflight: PreflightEvidence
    dataset_name: str | None = None
    dataset_id: str | None = None
    item_id: str | None = None
    experiment_id: str | None = None
    experiment_name: str
    span_trace_id: str | None = None
    experiment_found: bool = False
    linkage_signal: str | None = None
    error: str | None = None


def _write_evidence(path: Path, evidence: B3ExperimentLinkageEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")


def _build_probe_span(dataset: DatasetProbe, experiment_id: str) -> SpanModel:
    now = datetime.now(UTC).timestamp()
    span = SpanModel(
        trace_id=uuid4().hex,
        span_id=uuid4().hex[:16],
        name="b3.linkage.probe",
        start_time=now,
        end_time=now,
    )
    return attach_experiment_attrs(
        span,
        ExperimentLinkage(
            experiment_id=experiment_id,
            experiment_name=_EXPERIMENT_NAME,
            dataset_id=dataset.dataset_id,
            item_id=dataset.item_id,
        ),
    )


async def run_gate(
    *,
    evidence_path: Path = _EVIDENCE_PATH,
) -> B3ExperimentLinkageEvidence:
    checked_at = datetime.now(UTC)
    preflight = _run_preflight()
    if preflight.missing:
        evidence = B3ExperimentLinkageEvidence(
            passed=False,
            checked_at=checked_at,
            preflight=preflight,
            experiment_name=_EXPERIMENT_NAME,
            error="preflight failed: " + ", ".join(preflight.missing),
        )
        _write_evidence(evidence_path, evidence)
        return evidence

    dataset: DatasetProbe | None = None
    experiment_id: str | None = None
    span: SpanModel | None = None
    lookup = LinkageLookup(experiment_found=False, linkage_signal=None)
    try:
        dataset = await _create_probe_dataset()
        experiment_id = await _mint_experiment_id(dataset, _EXPERIMENT_NAME)
        span = _build_probe_span(dataset, experiment_id)
        await _emit_probe_span(span)
        credentials = LangfuseCredentials.from_env()
        if credentials is None:
            raise KeyError("Langfuse credentials are required")
        host = credentials.host if credentials.host is not None else "http://localhost:3000"
        lookup = await _poll_linkage(
            ExperimentQuery(
                host=host,
                public_key=credentials.public_key,
                secret_key=credentials.secret_key,
                experiment_name=_EXPERIMENT_NAME,
                dataset_id=dataset.dataset_id,
                from_start_time=checked_at - _WINDOW,
                to_start_time=datetime.now(UTC) + _WINDOW,
            )
        )
        if not lookup.experiment_found:
            raise GateError(
                step="experiment_poll",
                detail=(
                    f"{_EXPERIMENT_NAME} was not found; emitted span "
                    f"trace_id={span.trace_id} span_id={span.span_id}"
                ),
            )
        if lookup.linkage_signal is None:
            raise GateError(
                step="linkage_assertion",
                detail=(
                    f"experiment had no linked items; emitted span "
                    f"trace_id={span.trace_id} span_id={span.span_id}"
                ),
            )
        evidence = B3ExperimentLinkageEvidence(
            passed=True,
            checked_at=checked_at,
            preflight=preflight,
            dataset_name=dataset.dataset_name,
            dataset_id=dataset.dataset_id,
            item_id=dataset.item_id,
            experiment_id=experiment_id,
            experiment_name=_EXPERIMENT_NAME,
            span_trace_id=span.trace_id,
            experiment_found=True,
            linkage_signal=lookup.linkage_signal,
        )
    except (GateError, ExperimentLinkageError, TimeoutError) as exc:
        match exc:
            case ExperimentLinkageError():
                error = f"stable_experiment_id failed: {exc}"
            case GateError() | TimeoutError():
                error = str(exc)
            case unreachable:
                assert_never(unreachable)
        evidence = B3ExperimentLinkageEvidence(
            passed=False,
            checked_at=checked_at,
            preflight=preflight,
            dataset_name=dataset.dataset_name if dataset is not None else None,
            dataset_id=dataset.dataset_id if dataset is not None else None,
            item_id=dataset.item_id if dataset is not None else None,
            experiment_id=experiment_id,
            experiment_name=_EXPERIMENT_NAME,
            span_trace_id=span.trace_id if span is not None else None,
            experiment_found=lookup.experiment_found,
            linkage_signal=lookup.linkage_signal,
            error=error,
        )
    _write_evidence(evidence_path, evidence)
    return evidence


@app.command()
def main() -> None:
    evidence = anyio.run(run_gate)
    typer.echo(evidence.model_dump_json(indent=2))
    if not evidence.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
