"""Self-hosted memory probe runner with F3 linkage and durable resume."""

from __future__ import annotations

from bot.eval.memory_harness import (
    build_memory_runtime_services,
    run_dream_until_exhausted,
)
from bot.eval.probes._harness_execution import (
    experiment_linkage,
    ingest_world,
    query_and_score,
)
from bot.eval.probes._harness_io import (
    append_checkpoint,
    capture_snapshot,
    load_checkpoints,
    load_snapshot,
)
from bot.eval.probes._harness_models import (
    DreamSnapshot,
    ExperimentItem,
    ExperimentSetup,
    HarnessStatus,
    MemorySnapshot,
    ProbeCheckpoint,
    ProbeHarnessConfig,
    ProbeHarnessError,
    ProbeHarnessResult,
    ProbeHarnessServices,
    ProbeItemStatus,
    ProbeRunRecord,
    ProbeScore,
    ScoreFn,
)
from bot.eval.probes.budget import BudgetConfig, BudgetedProvider, CostCapExceededError
from bot.eval.probes.generate import load_frozen_library
from modex_agent.trace.experiment_attrs import (
    ExperimentAttribute,
)


async def run_probe_harness(
    config: ProbeHarnessConfig,
    services: ProbeHarnessServices,
) -> ProbeHarnessResult:
    """Run ingest, snapshot, bare-answer prediction, and scoring sequentially."""
    world, _manifest = load_frozen_library(config.library_path, config.manifest_path)
    experiment = await services.experiment_setup(world, config.run_name)
    checkpoints = load_checkpoints(config.checkpoint_path)
    snapshot = load_snapshot(config.snapshot_path)
    prior_cost = sum(checkpoint.cost_usd for checkpoint in checkpoints)
    if snapshot is not None:
        prior_cost += snapshot.ingest_cost_usd
    remaining_cost = config.max_cost_usd - prior_cost
    if remaining_cost <= 0:
        return _result(HarnessStatus.COST_CAPPED, experiment, checkpoints, snapshot, prior_cost)

    provider = BudgetedProvider(
        services.provider,
        services.pricebook,
        BudgetConfig(
            max_cost_usd=remaining_cost,
            minimum_call_reserve_usd=config.minimum_call_reserve_usd,
        ),
    )
    bundle = await build_memory_runtime_services(config.workspace, provider)
    trace_store = bundle.runtime_services.trace_store
    if trace_store is None:
        await bundle.memory_system.close()
        raise ProbeHarnessError("probe harness requires an enabled trace store")

    status = HarnessStatus.COMPLETE
    try:
        bundle.memory_trace_hook.experiment_linkage = experiment_linkage(
            experiment, experiment.ingest_item_id
        )
        if snapshot is None:
            try:
                ingested_turns = await ingest_world(world, bundle)
                dream_summary = await run_dream_until_exhausted(
                    bundle.memory_system,
                    dream_engine=bundle.dream_engine,
                )
            except CostCapExceededError:
                status = HarnessStatus.COST_CAPPED
            else:
                snapshot = await capture_snapshot(
                    path=config.snapshot_path,
                    world=world,
                    bundle=bundle,
                    dream=DreamSnapshot(
                        iterations=dream_summary.iterations,
                        exhausted=dream_summary.exhausted,
                        stalled=dream_summary.stalled,
                    ),
                    ingested_turns=ingested_turns,
                    ingest_cost_usd=provider.spent_cost_usd,
                )

        if status is HarnessStatus.COMPLETE and snapshot is not None:
            completed = {checkpoint.probe_id for checkpoint in checkpoints}
            cost_cursor = provider.spent_cost_usd
            for probe in world.probes:
                if probe.probe_id in completed:
                    continue
                bundle.memory_trace_hook.experiment_linkage = experiment_linkage(
                    experiment, experiment.item_id_for(probe.probe_id)
                )
                try:
                    record, score = await query_and_score(
                        probe,
                        snapshot,
                        experiment,
                        bundle,
                        provider,
                        services,
                        config.answer_max_output_tokens,
                    )
                except CostCapExceededError:
                    status = HarnessStatus.COST_CAPPED
                    break
                except Exception as exc:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
                    checkpoint = ProbeCheckpoint(
                        probe_id=probe.probe_id,
                        status=ProbeItemStatus.FAILED,
                        cost_usd=round(provider.spent_cost_usd - cost_cursor, 12),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                else:
                    checkpoint = ProbeCheckpoint(
                        probe_id=probe.probe_id,
                        status=ProbeItemStatus.COMPLETE,
                        cost_usd=round(provider.spent_cost_usd - cost_cursor, 12),
                        record=record,
                        score=score,
                    )
                append_checkpoint(config.checkpoint_path, checkpoint)
                checkpoints.append(checkpoint)
                completed.add(probe.probe_id)
                cost_cursor = provider.spent_cost_usd
    finally:
        await bundle.memory_system.close()
        trace_store.close()

    spent = prior_cost + provider.spent_cost_usd
    return _result(status, experiment, checkpoints, snapshot, spent)


def _result(
    status: HarnessStatus,
    experiment: ExperimentSetup,
    checkpoints: list[ProbeCheckpoint],
    snapshot: MemorySnapshot | None,
    spent_cost_usd: float,
) -> ProbeHarnessResult:
    return ProbeHarnessResult(
        status=status,
        experiment_id=experiment.experiment_id,
        completed_probe_ids=[checkpoint.probe_id for checkpoint in checkpoints],
        records=[checkpoint.record for checkpoint in checkpoints if checkpoint.record is not None],
        failures=[
            checkpoint for checkpoint in checkpoints if checkpoint.status is ProbeItemStatus.FAILED
        ],
        ingested_turns=0 if snapshot is None else snapshot.ingested_turns,
        spent_cost_usd=round(spent_cost_usd, 12),
    )


__all__ = [
    "ExperimentAttribute",
    "ExperimentItem",
    "ExperimentSetup",
    "HarnessStatus",
    "MemorySnapshot",
    "ProbeCheckpoint",
    "ProbeHarnessConfig",
    "ProbeHarnessError",
    "ProbeHarnessResult",
    "ProbeHarnessServices",
    "ProbeItemStatus",
    "ProbeRunRecord",
    "ProbeScore",
    "ScoreFn",
    "run_probe_harness",
]
