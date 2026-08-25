"""Typed contracts for standalone experiment re-judging."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from bot.eval.dataset_curator import DatasetCurator
from bot.eval.judge.runner import JudgeRunner
from modex_agent.core.provider import LLMProvider
from modex_agent.trace.langfuse_query import LangfuseClient
from modex_agent.trace.score_injector import L2ScoreInjector


class ExperimentWindow(BaseModel):
    """Typed time window returned by the Langfuse experiments API."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    name: str
    dataset_id: str = Field(alias="datasetId")
    start_time: datetime = Field(alias="startTime")
    end_time: datetime = Field(alias="endTime")


class JudgePassConfig(BaseModel):
    """Validated selection and output controls for one standalone judge pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment: str
    rubric_set: str = "general-agent"
    dataset: str | None = None
    limit: PositiveInt | None = None
    repeats: PositiveInt = 1
    archive_root: Path = Path("evals/runs/judge")


class JudgePassReport(BaseModel):
    """Aggregate measurements emitted by one judge pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    judged_count: int
    mean_score: float
    agreement_rate: float
    rubric_version: str


class JudgePassResources(BaseModel):
    """Concrete runtime dependencies for one judge pass."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    curator: DatasetCurator
    observation_client: LangfuseClient
    runner: JudgeRunner
    injector: L2ScoreInjector
    emit: Callable[[str], None]


class JudgePassEnvironment(BaseModel):
    """Credentials, provider, and output sink owned by the CLI boundary."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    host: str
    public_key: str
    secret_key: str
    provider: LLMProvider
    emit: Callable[[str], None]
