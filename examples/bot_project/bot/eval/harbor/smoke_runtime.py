"""Harbor and Langfuse execution for the manual B6 smoke gate."""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Final

import anyio
import httpx
from anyio import to_thread
from langfuse import Langfuse
from langfuse.api.core.api_error import ApiError
from pydantic import BaseModel, ConfigDict, ValidationError

from bot.eval.evalenv import LangfuseCredentials
from bot.eval.harbor.agent import InstallExecutionResult
from bot.eval.harbor.host_cli import collect_job
from bot.eval.harbor.host_runtime import (
    RunTrialRequest,
    SubprocessExecutionPlane,
    mint_stable_experiment_id,
    run_trial,
)
from bot.eval.harbor.pool_mode_types import PoolTaskResultArtifact, PoolUsageArtifact
from bot.eval.harbor.verdict_collector import (
    VerdictCollectionError,
    read_official_results,
    read_trial_trace_map,
)
from modex_agent.trace.experiment_attrs import ExperimentLinkageError
from modex_agent.trace.langfuse_query import LangfuseClient, LangfuseQueryError

_DATASET_NAME: Final = "terminalbench-b6-smoke-v1"
_OVERLAY_PATH: Final = Path("bot/eval/harbor/docker-compose.uv.yml")


class SmokeGateError(RuntimeError):
    pass


class SmokeMode(StrEnum):
    BARE = "bare"
    POOL = "pool"


class SmokeDatasetItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_path: Path
    item_id: str


class SmokeDataset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str
    items: tuple[SmokeDatasetItem, SmokeDatasetItem]


class SmokeRunRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_paths: tuple[Path, Path]
    run_id: str
    model: str
    timeout_multiplier: float
    jobs_dir: Path
    mode: SmokeMode = SmokeMode.BARE


class SmokeRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_name: str
    job_dirs: tuple[str, ...]
    trace_ids: tuple[str, ...]
    verdicts: tuple[float, ...]
    score_count: int
    install_seconds: tuple[float, ...] = ()
    child_sessions: tuple[str, ...] = ()
    delegation_counts: tuple[int, ...] = ()


def _create_dataset(task_paths: tuple[Path, Path]) -> SmokeDataset:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    client = Langfuse(
        base_url=host,
        public_key=credentials.public_key,
        secret_key=credentials.secret_key,
        timeout=10,
        tracing_enabled=False,
    )
    try:
        dataset = client.create_dataset(
            name=_DATASET_NAME,
            description="Two-task local B6 Harbor smoke dataset.",
        )
        first_path, second_path = task_paths
        items = (
            SmokeDatasetItem(
                task_path=first_path,
                item_id=client.create_dataset_item(
                    dataset_name=_DATASET_NAME,
                    input={"task": first_path.name, "path": first_path.as_posix()},
                ).id,
            ),
            SmokeDatasetItem(
                task_path=second_path,
                item_id=client.create_dataset_item(
                    dataset_name=_DATASET_NAME,
                    input={"task": second_path.name, "path": second_path.as_posix()},
                ).id,
            ),
        )
        return SmokeDataset(dataset_id=dataset.id, items=items)
    finally:
        client.shutdown()


async def _read_back_verdict_count(trace_ids: tuple[str, ...]) -> int:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    client = LangfuseClient(
        host,
        credentials.public_key,
        credentials.secret_key,
    )
    try:
        count = 0
        for attempt in range(10):
            count = 0
            for trace_id in trace_ids:
                scores, _cursor = await client.get_scores(
                    fields="core,details,subject",
                    trace_id=trace_id,
                    name="verdict_terminalbench",
                )
                count += len(scores)
            if count == len(trace_ids):
                return count
            if attempt < 9:
                await anyio.sleep(1)
        return count
    finally:
        await client.close()


def _read_pool_trial_evidence(job_dir: Path) -> tuple[float, tuple[str, ...], int]:
    """Read install duration and delegation evidence from a job's latest trial."""
    install_path = sorted(job_dir.glob("*/agent/install-result.json"))
    usage_path = sorted(job_dir.glob("*/agent/usage.json"))
    result_path = sorted(job_dir.glob("*/agent/result.json"))
    if not (install_path and usage_path and result_path):
        raise SmokeGateError(f"pool trial artifacts missing under {job_dir}")
    install = InstallExecutionResult.model_validate_json(
        install_path[-1].read_text(encoding="utf-8")
    )
    usage = PoolUsageArtifact.model_validate_json(usage_path[-1].read_text(encoding="utf-8"))
    result = PoolTaskResultArtifact.model_validate_json(result_path[-1].read_text(encoding="utf-8"))
    return (
        install.duration_seconds,
        tuple(result.child_sessions),
        usage.delegation.delegation_count,
    )


async def run_smoke(request: SmokeRunRequest) -> SmokeRunResult:
    """Run two Harbor tasks and read their official Langfuse verdict scores."""
    experiment_name = f"terminalbench.{request.run_id}"
    if request.mode is SmokeMode.POOL:
        # run_trial forwards pool env from os.environ; the gate's --mode is authoritative.
        os.environ["MODEX_AGENT_MODE"] = SmokeMode.POOL.value
    try:
        dataset = await to_thread.run_sync(_create_dataset, request.task_paths)
        job_dirs: list[str] = []
        trace_ids: list[str] = []
        verdicts: list[float] = []
        install_seconds: list[float] = []
        child_sessions: list[str] = []
        delegation_counts: list[int] = []
        for index, item in enumerate(dataset.items, start=1):
            job_name = f"{request.run_id}-{index}"
            trial = await run_trial(
                RunTrialRequest(
                    task_path=item.task_path,
                    jobs_dir=request.jobs_dir,
                    job_name=job_name,
                    experiment_name=experiment_name,
                    dataset_id=dataset.dataset_id,
                    item_id=item.item_id,
                    model=request.model,
                    memory_namespace=experiment_name,
                    timeout_multiplier=request.timeout_multiplier,
                    compose_overlay=_OVERLAY_PATH,
                ),
                SubprocessExecutionPlane(),
                mint_stable_experiment_id,
            )
            if trial.result.exit_code != 0:
                raise SmokeGateError(trial.result.stderr or trial.result.stdout)
            job_dir = request.jobs_dir / job_name
            await collect_job(job_dir, "terminalbench.official.v1", job_dir.as_posix())
            mapping = read_trial_trace_map(job_dir)
            results = read_official_results(job_dir)
            job_dirs.append(job_dir.as_posix())
            trace_ids.extend(entry.trace_id for entry in mapping.entries)
            verdicts.extend(result.value for result in results)
            if request.mode is SmokeMode.POOL:
                seconds, children, count = _read_pool_trial_evidence(job_dir)
                install_seconds.append(seconds)
                child_sessions.extend(children)
                delegation_counts.append(count)
        score_count = await _read_back_verdict_count(tuple(trace_ids))
        if score_count != 2:
            raise SmokeGateError(f"expected 2 verdict scores, received {score_count}")
        return SmokeRunResult(
            experiment_name=experiment_name,
            job_dirs=tuple(job_dirs),
            trace_ids=tuple(trace_ids),
            verdicts=tuple(verdicts),
            score_count=score_count,
            install_seconds=tuple(install_seconds),
            child_sessions=tuple(child_sessions),
            delegation_counts=tuple(delegation_counts),
        )
    except (
        ApiError,
        ExperimentLinkageError,
        KeyError,
        LangfuseQueryError,
        OSError,
        ValidationError,
        VerdictCollectionError,
        httpx.HTTPError,
    ) as error:
        raise SmokeGateError(str(error)) from error


__all__ = [
    "SmokeGateError",
    "SmokeMode",
    "SmokeRunRequest",
    "SmokeRunResult",
    "run_smoke",
]
