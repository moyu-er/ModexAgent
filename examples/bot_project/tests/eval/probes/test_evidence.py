from __future__ import annotations

from datetime import UTC, datetime

from bot.eval.memory_metric_models import UtilizationClass
from bot.eval.probes._harness_models import (
    HarnessStatus,
    ProbeHarnessResult,
    ProbeRunRecord,
    ProbeScore,
)
from bot.eval.probes.dual_arm import NomemoryProbeResult, NomemoryRunResult
from bot.eval.probes.evidence import (
    B5EvidenceInput,
    ExperimentApiExcerpt,
    ServicePreflight,
    assemble_b5_evidence,
)
from bot.eval.probes.sampler import LibraryScale, config_for_scale, sample_world
from bot.eval.probes.schema import Probe, WorldSpec


def test_assemble_b5_evidence_reports_type_scores_dual_delta_compare_and_cost() -> None:
    # Given
    world = sample_world(seed=21, config=config_for_scale(LibraryScale.SMOKE))
    dual = [probe for probe in world.probes if probe.dual_arm]
    memory_records = [_passing_record(world, probe) for probe in dual]
    control_records = [
        _control(memory_records[0], answer="unsupported", passed=False),
        _control(memory_records[1], answer=dual[1].expected_answers[0], passed=True),
    ]
    harness_result = ProbeHarnessResult(
        status=HarnessStatus.COMPLETE,
        experiment_id="experiment-1",
        completed_probe_ids=[probe.probe_id for probe in dual],
        records=memory_records,
        failures=[],
        ingested_turns=40,
        spent_cost_usd=0.42,
    )

    # When
    evidence = assemble_b5_evidence(
        B5EvidenceInput(
            run_name="memory-probes.smoke-1",
            max_cost_usd=1.0,
            world=world,
            harness_result=harness_result,
            nomemory_result=NomemoryRunResult(
                results=control_records,
                spent_cost_usd=0.08,
                cost_capped=False,
            ),
            experiment_compare=[
                ExperimentApiExcerpt(
                    experiment_id="experiment-1",
                    name="memory-probes.smoke-1",
                    dataset_id="dataset-1",
                    item_count=3,
                    start_time="2026-08-21T00:00:00Z",
                    end_time="2026-08-21T00:01:00Z",
                )
            ],
            preflight=ServicePreflight(
                langfuse_health=True,
                collector_port=True,
                missing=[],
            ),
        )
    )

    # Then
    by_type = {entry.probe_type: entry.tally for entry in evidence.type_scores.by_type}
    assert by_type[dual[0].probe_type].passed >= 1
    labels = {record.answer_id: record.label for record in evidence.dual_arm.records}
    assert labels[dual[0].probe_id] is UtilizationClass.BENEFICIAL
    assert labels[dual[1].probe_id] is UtilizationClass.NEUTRAL
    assert evidence.dual_arm.counts.beneficial == 1
    assert evidence.dual_arm.counts.neutral == 1
    assert evidence.experiment_compare[0].item_count == 3
    assert evidence.actual_cost_usd == 0.5
    assert evidence.within_cost_cap is True


def _passing_record(world: WorldSpec, probe: Probe) -> ProbeRunRecord:
    facts = {fact.fact_id: fact for fact in world.facts}
    expected_surfaces = [
        surface for fact_id in probe.fact_ids for surface in facts[fact_id].surface_refs
    ]
    answer = " ".join(probe.expected_answers)
    now = datetime.now(UTC)
    return ProbeRunRecord(
        probe=probe,
        answer=answer,
        assembled_context=" ".join(expected_surfaces),
        trace_id=f"trace-{probe.probe_id}",
        span_id=f"span-{probe.probe_id}",
        started_at=now,
        completed_at=now,
        snapshot_captured_at=now,
    )


def _control(
    memory_record: ProbeRunRecord,
    *,
    answer: str,
    passed: bool,
) -> NomemoryProbeResult:
    record = memory_record.model_copy(
        update={
            "answer": answer,
            "assembled_context": "",
            "trace_id": f"nomemory-{memory_record.trace_id}",
            "span_id": f"nomemory-{memory_record.span_id}",
        }
    )
    return NomemoryProbeResult(
        record=record,
        score=ProbeScore(
            name=f"memory_probe_{record.probe.probe_type.value}",
            value=passed,
            data_type="BOOLEAN",
        ),
        cost_usd=0.04,
    )
