"""Manual B7 dual-arm sentinel gate and evidence writer."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Final

import anyio
import typer
from anyio import to_thread
from evals.sentinel.tasks import MEMORY_CHAIN_V1_CHAIN
from langfuse import Langfuse
from pydantic import BaseModel, ConfigDict, Field

from bot.eval.evalenv import LangfuseCredentials
from bot.eval.live_gates.b3_linkage_runtime import (
    ExperimentQuery,
    GateError,
    PreflightEvidence,
    poll_linkage,
    run_preflight,
)
from bot.eval.probes.budget import BudgetConfig, BudgetedProvider
from bot.eval.sentinel.execution import (
    SENTINEL_VERDICT_SCORE,
    SENTINEL_VERDICT_VERSION,
    HostSentinelExecutionPlane,
    SentinelTraceRecord,
)
from bot.eval.sentinel.orchestrator import (
    SentinelOrchestrator,
    SentinelRunRequest,
    SentinelRunResult,
)
from bot.eval.sentinel.report import SentinelDifferenceReport
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.trace.experiment_attrs import ExperimentLinkage, stable_experiment_id
from modex_agent.trace.langfuse_query import LangfuseClient, parse_provenance
from modex_agent.trace.pricing import load_pricebook
from modex_agent.trace.score_injector import L2ScoreInjector

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
_MODEL: Final = "step-3.7-flash"
_EVIDENCE_PATH: Final = Path("evals/evidence/b7_sentinel.json")
_EXPECTED_VERDICT_SCORES: Final = len(MEMORY_CHAIN_V1_CHAIN.tasks) * 2


class SentinelGateEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    checked_at: datetime
    run_id: str
    seed: int
    actual_cost_usd: float = Field(ge=0)
    max_cost_usd: float = Field(gt=0)
    experiments: tuple[str, str]
    report: SentinelDifferenceReport
    trace_ids: tuple[str, ...] = ()
    trace_records: tuple[SentinelTraceRecord, ...] = ()
    visible_experiments: tuple[str, ...] = ()
    verdict_score_count: int = Field(default=0, ge=0)
    preflight: PreflightEvidence | None = None
    error: str | None = None


def build_gate_evidence(
    result: SentinelRunResult,
    *,
    actual_cost_usd: float,
    max_cost_usd: float,
    trace_ids: tuple[str, ...] = (),
    trace_records: tuple[SentinelTraceRecord, ...] = (),
    visible_experiments: tuple[str, ...] = (),
    verdict_score_count: int = 0,
    require_visibility: bool = False,
    preflight: PreflightEvidence | None = None,
) -> SentinelGateEvidence:
    """Apply the hard cost cap and strict directional B7 criterion."""
    cost_ok = actual_cost_usd <= max_cost_usd
    directional = result.report.difference.success_count_delta > 0
    experiments = (result.arms[0].experiment_name, result.arms[1].experiment_name)
    visibility_ok = not require_visibility or (
        frozenset(visible_experiments) == frozenset(experiments)
        and verdict_score_count == _EXPECTED_VERDICT_SCORES
    )
    error = None
    if not cost_ok:
        error = "cost cap exceeded"
    elif not directional:
        error = "memory arm did not strictly outperform nomemory arm"
    elif not visibility_ok:
        error = "two-arm trace/experiment visibility incomplete"
    return SentinelGateEvidence(
        passed=cost_ok and directional and visibility_ok,
        checked_at=datetime.now(UTC),
        run_id=result.run_id,
        seed=result.seed,
        actual_cost_usd=actual_cost_usd,
        max_cost_usd=max_cost_usd,
        experiments=experiments,
        report=result.report,
        trace_ids=trace_ids,
        trace_records=trace_records,
        visible_experiments=visible_experiments,
        verdict_score_count=verdict_score_count,
        preflight=preflight,
        error=error,
    )


def _linkages(run_id: str) -> dict[tuple[str, str], ExperimentLinkage]:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    client = Langfuse(
        host=host,
        public_key=credentials.public_key,
        secret_key=credentials.secret_key,
        tracing_enabled=False,
    )
    values: dict[tuple[str, str], ExperimentLinkage] = {}
    try:
        dataset_name = f"memory-chain-v1-{run_id}"
        dataset = client.create_dataset(name=dataset_name)
        for arm in ("memory", "nomemory"):
            experiment_name = f"memory-chain-v1.{run_id}.{arm}"
            for task in MEMORY_CHAIN_V1_CHAIN.tasks:
                item = client.create_dataset_item(
                    dataset_name=dataset_name,
                    input={"task_id": task.task_id, "arm": arm},
                )
                experiment_id = stable_experiment_id(
                    host=host,
                    public_key=credentials.public_key,
                    secret_key=credentials.secret_key,
                    dataset_id=dataset.id,
                    item_id=item.id,
                    run_name=experiment_name,
                )
                values[(arm, task.task_id)] = ExperimentLinkage(
                    experiment_id=experiment_id,
                    experiment_name=experiment_name,
                    dataset_id=dataset.id,
                    item_id=item.id,
                )
    finally:
        client.shutdown()
    return values


def _score_injector() -> L2ScoreInjector:
    host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000").rstrip("/")
    return L2ScoreInjector(
        ingestion_url=f"{host}/api/public/ingestion",
        headers={
            "Authorization": f"Basic {os.environ['LANGFUSE_BASIC_AUTH']}",
            "x-langfuse-ingestion-version": "4",
        },
    )


async def _read_back_visibility(
    records: tuple[SentinelTraceRecord, ...],
    links: dict[tuple[str, str], ExperimentLinkage],
    started_at: datetime,
    run_ref: str,
) -> tuple[tuple[str, ...], int]:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    visible: list[str] = []
    for arm in ("memory", "nomemory"):
        first_record = next(record for record in records if record.arm.value == arm)
        linkage = links[(arm, first_record.task_id)]
        lookup = await poll_linkage(
            ExperimentQuery(
                host=host,
                public_key=credentials.public_key,
                secret_key=credentials.secret_key,
                experiment_name=linkage.experiment_name,
                dataset_id=linkage.dataset_id,
                from_start_time=started_at - timedelta(minutes=1),
                to_start_time=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
        if lookup.experiment_found and lookup.linkage_signal is not None:
            visible.append(linkage.experiment_name)

    client = LangfuseClient(
        host,
        credentials.public_key,
        credentials.secret_key,
    )
    score_count = 0
    try:
        for attempt in range(10):
            score_count = 0
            for record in records:
                scores, _cursor = await client.get_scores(
                    fields="core,details,subject",
                    trace_id=record.trace_id,
                    name=SENTINEL_VERDICT_SCORE,
                )
                for score in scores:
                    provenance = parse_provenance(score.comment)
                    if (
                        provenance is not None
                        and provenance.scorer == "verifier"
                        and provenance.version == SENTINEL_VERDICT_VERSION
                        and provenance.report_source == "official_harness"
                        and provenance.run_ref == run_ref
                    ):
                        score_count += 1
            if score_count == len(records):
                break
            if attempt < 9:
                await anyio.sleep(1)
    finally:
        await client.close()
    return tuple(visible), score_count


async def run_gate(run_id: str, max_cost: float, seed: int) -> SentinelGateEvidence:
    started_at = datetime.now(UTC)
    run_dir = Path("evals/runs/sentinel") / run_id
    run_ref = run_dir.as_posix()
    preflight = await to_thread.run_sync(run_preflight)
    if preflight.missing:
        raise GateError(step="preflight", detail=", ".join(preflight.missing))
    links = await to_thread.run_sync(_linkages, run_id)
    provider = BudgetedProvider(
        create_llm_provider(
            LLMConfig(
                model=_MODEL,
                api_key=os.environ.get("LLM_API_KEY") or "",
                base_url=os.environ.get("LLM_BASE_URL") or "",
            )
        ),
        load_pricebook(yml_path=Path("config/model_prices.yml")),
        BudgetConfig(max_cost_usd=max_cost, minimum_call_reserve_usd=0.01),
    )
    score_injector = _score_injector()
    execution = HostSentinelExecutionPlane(
        provider,
        lambda instance: links[(instance.arm.value, instance.task.task_id)],
        run_ref=run_ref,
        score_injector=score_injector,
    )
    try:
        result = await SentinelOrchestrator(execution).run(
            SentinelRunRequest(
                run_id=run_id,
                seed=seed,
                memory_root=run_dir,
            )
        )
    finally:
        await score_injector.aclose()
    trace_records = tuple(execution.trace_records)
    visible_experiments, verdict_score_count = await _read_back_visibility(
        trace_records, links, started_at, run_ref
    )
    evidence = build_gate_evidence(
        result,
        actual_cost_usd=provider.spent_cost_usd,
        max_cost_usd=max_cost,
        trace_ids=tuple(record.trace_id for record in trace_records),
        trace_records=trace_records,
        visible_experiments=visible_experiments,
        verdict_score_count=verdict_score_count,
        require_visibility=True,
        preflight=preflight,
    )
    _EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _EVIDENCE_PATH.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    return evidence


@app.command()
def main(
    run_id: Annotated[str, typer.Option("--run-id")],
    max_cost: Annotated[float, typer.Option("--max-cost", min=0.01)],
    seed: Annotated[int, typer.Option("--seed")],
) -> None:
    evidence = anyio.run(run_gate, run_id, max_cost, seed)
    typer.echo(evidence.model_dump_json(indent=2))
    if not evidence.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()


__all__ = ["SentinelGateEvidence", "build_gate_evidence", "run_gate"]
