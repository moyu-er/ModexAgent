"""Data contract and pool-mode host validation for the B6 smoke gate."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from bot.eval.harbor.model_source import ModelSource
from bot.eval.harbor.pool_budget import (
    DEFAULT_POOL_BUDGET_USD,
    POOL_BUDGET_ENV,
    PoolBudgetEnvironmentError,
    pool_budget_config_from_env,
)
from bot.eval.harbor.smoke_runtime import SmokeMode
from modex_agent.core.constants import ReasoningEffort

REQUIRED_LANGFUSE_CONTAINERS: Final = frozenset(
    {
        "modex-langfuse-web",
        "modex-langfuse-worker",
        "modex-langfuse-clickhouse",
        "modex-langfuse-minio",
        "modex-langfuse-redis",
    }
)
_EVIDENCE_PATH: Final = Path("evals/evidence/b6_smoke.json")
_POOL_EVIDENCE_PATH: Final = Path("evals/evidence/b6_pool_smoke.json")
_REQUIRED_POOL_ENV: Final = ("LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL")


class SmokeCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class SmokePreflight(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    docker_daemon: bool
    langfuse_stack: bool
    missing: tuple[str, ...]


class SmokeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_paths: tuple[Path, Path]
    run_id: str
    model: str | None = None
    timeout_multiplier: float
    jobs_dir: Path
    mode: SmokeMode = SmokeMode.BARE
    evidence_path: Path = _EVIDENCE_PATH
    model_yml: Path | None = None


class B6SmokeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Literal["b6_smoke"] = "b6_smoke"
    passed: bool
    checked_at: datetime
    preflight: SmokePreflight
    run_id: str
    experiment_name: str
    task_paths: tuple[str, ...]
    model: str | None = None
    model_source: ModelSource | None = None
    temperature: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    job_dirs: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    verdicts: tuple[float, ...] = ()
    score_count: int = 0
    error: str | None = None


class B6PoolSmokeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: Literal["b6_pool_smoke"] = "b6_pool_smoke"
    mode: Literal["pool"] = "pool"
    passed: bool
    checked_at: datetime
    preflight: SmokePreflight
    run_id: str
    experiment_name: str
    task_paths: tuple[str, ...]
    pool_name: str
    budget_usd: float
    model: str | None = None
    model_source: ModelSource | None = None
    temperature: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    job_dirs: tuple[str, ...] = ()
    trace_ids: tuple[str, ...] = ()
    verdicts: tuple[float, ...] = ()
    score_count: int = 0
    install_seconds: tuple[float, ...] = ()
    child_sessions: tuple[str, ...] = ()
    delegation_counts: tuple[int, ...] = ()
    error: str | None = None


def missing_pool_env() -> tuple[str, ...]:
    missing = tuple(
        f"env:{name}" for name in _REQUIRED_POOL_ENV if not os.environ.get(name)
    )
    try:
        pool_budget_config_from_env()
    except PoolBudgetEnvironmentError:
        missing += (f"env:{POOL_BUDGET_ENV}",)
    return missing


def pool_budget_usd() -> float:
    try:
        return pool_budget_config_from_env().max_cost_usd
    except PoolBudgetEnvironmentError:
        return DEFAULT_POOL_BUDGET_USD


__all__ = [
    "B6PoolSmokeEvidence",
    "B6SmokeEvidence",
    "REQUIRED_LANGFUSE_CONTAINERS",
    "SmokeCommandResult",
    "SmokePreflight",
    "SmokeRequest",
    "missing_pool_env",
    "pool_budget_usd",
]
