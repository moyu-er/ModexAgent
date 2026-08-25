"""Pure B5 evidence assembly over memory and no-memory probe outcomes."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.memory_metric_models import (
    ProbeDeltaRecord,
    UtilizationDelta,
)
from bot.eval.memory_metrics import reduce_memory_spans
from bot.eval.probes._harness_models import (
    HarnessStatus,
    ProbeHarnessResult,
    ProbeRunRecord,
)
from bot.eval.probes.dual_arm import (
    NomemoryRunResult,
    classify_utilization,
)
from bot.eval.probes.schema import Fact, WorldSpec
from bot.eval.probes.scoring import (
    ProbeAnswer,
    ProbeScore,
    ProbeScoringReport,
    score_probe,
    summarize_probe_scores,
)


class EvidenceStatus(StrEnum):
    """Completeness of the local B5 dispatch evidence."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class ServicePreflight(BaseModel):
    """Bounded live-service checks captured before dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    langfuse_health: bool
    collector_port: bool
    missing: list[str]


class ExperimentApiExcerpt(BaseModel):
    """Stable fields copied from one experiments API response item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    name: str
    dataset_id: str | None
    item_count: int = Field(ge=0)
    start_time: str | None
    end_time: str | None


class DualArmEvidence(BaseModel):
    """Per-probe labels and the ticket-10 four-class count shape."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_count: int = Field(ge=0)
    completed_count: int = Field(ge=0)
    records: list[ProbeDeltaRecord]
    counts: UtilizationDelta


class B5EvidenceInput(BaseModel):
    """Typed input to the side-effect-free B5 evidence reducer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_name: str = Field(min_length=1)
    max_cost_usd: float = Field(gt=0)
    world: WorldSpec
    harness_result: ProbeHarnessResult
    nomemory_result: NomemoryRunResult
    experiment_compare: list[ExperimentApiExcerpt]
    preflight: ServicePreflight


class B5Evidence(BaseModel):
    """Durable receipt written only by a real manual B5 dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["b5_first_run.v1"] = "b5_first_run.v1"
    run_name: str
    status: EvidenceStatus
    preflight: ServicePreflight
    type_scores: ProbeScoringReport
    dual_arm: DualArmEvidence
    experiment_compare_api_path: Literal["/api/public/experiments"] = "/api/public/experiments"
    experiment_compare: list[ExperimentApiExcerpt]
    completed_probe_count: int = Field(ge=0)
    failed_probe_count: int = Field(ge=0)
    actual_cost_usd: float = Field(ge=0)
    max_cost_usd: float = Field(gt=0)
    within_cost_cap: bool


def assemble_b5_evidence(source: B5EvidenceInput) -> B5Evidence:
    """Reduce harness and control results without file or network access."""
    memory_scores = _score_records(source.world, source.harness_result.records)
    control_scores = _score_records(
        source.world,
        [item.record for item in source.nomemory_result.results],
    )
    memory_by_id = {score.probe_id: score for score in memory_scores}
    control_by_id = {score.probe_id: score for score in control_scores}
    dual_probes = [probe for probe in source.world.probes if probe.dual_arm]
    labels = [
        ProbeDeltaRecord(
            answer_id=probe.probe_id,
            label=classify_utilization(memory_score.passed, control_score.passed),
        )
        for probe in dual_probes
        if (memory_score := memory_by_id.get(probe.probe_id)) is not None
        and memory_score.passed is not None
        and (control_score := control_by_id.get(probe.probe_id)) is not None
        and control_score.passed is not None
    ]
    utilization = reduce_memory_spans([], probe_records=labels).utilization_delta
    counts = utilization or UtilizationDelta()
    actual_cost = round(
        source.harness_result.spent_cost_usd + source.nomemory_result.spent_cost_usd,
        12,
    )
    complete = (
        source.harness_result.status is HarnessStatus.COMPLETE
        and not source.nomemory_result.cost_capped
        and len(labels) == len(dual_probes)
        and bool(source.experiment_compare)
    )
    return B5Evidence(
        run_name=source.run_name,
        status=EvidenceStatus.COMPLETE if complete else EvidenceStatus.PARTIAL,
        preflight=source.preflight,
        type_scores=summarize_probe_scores(memory_scores),
        dual_arm=DualArmEvidence(
            expected_count=len(dual_probes),
            completed_count=len(labels),
            records=labels,
            counts=counts,
        ),
        experiment_compare=source.experiment_compare,
        completed_probe_count=len(source.harness_result.completed_probe_ids),
        failed_probe_count=len(source.harness_result.failures),
        actual_cost_usd=actual_cost,
        max_cost_usd=source.max_cost_usd,
        within_cost_cap=actual_cost <= source.max_cost_usd,
    )


def score_record(world: WorldSpec, record: ProbeRunRecord) -> ProbeScore:
    """Apply the frozen ticket-23 scorer to one harness record."""
    facts_by_id = {fact.fact_id: fact for fact in world.facts}
    return score_probe(
        ProbeAnswer(
            probe_id=record.probe.probe_id,
            probe_type=record.probe.probe_type,
            answer=record.answer,
            injected_context=record.assembled_context,
            expected_evidence=_facts(record.probe.fact_ids, facts_by_id),
            forbidden_evidence=_facts(record.probe.forbidden_fact_ids, facts_by_id),
        )
    )


def _score_records(world: WorldSpec, records: list[ProbeRunRecord]) -> list[ProbeScore]:
    return [score_record(world, record) for record in records]


def _facts(fact_ids: list[str], facts_by_id: dict[str, Fact]) -> list[Fact]:
    return [facts_by_id[fact_id] for fact_id in fact_ids]


__all__ = [
    "B5Evidence",
    "B5EvidenceInput",
    "DualArmEvidence",
    "EvidenceStatus",
    "ExperimentApiExcerpt",
    "ServicePreflight",
    "assemble_b5_evidence",
    "score_record",
]
