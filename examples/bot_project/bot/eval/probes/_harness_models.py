"""Typed records crossing memory probe harness boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.schema import Probe, WorldSpec
from modex_agent.core.provider import LLMProvider
from modex_agent.trace.pricing import PriceBook
from modex_agent.trace.score_injector import L2ScoreInjector


class ProbeHarnessError(RuntimeError):
    """The probe harness cannot satisfy a required runtime invariant."""


class HarnessStatus(StrEnum):
    """Terminal state of one harness invocation."""

    COMPLETE = "complete"
    COST_CAPPED = "cost_capped"


class ProbeItemStatus(StrEnum):
    """Checkpointed outcome of one probe."""

    COMPLETE = "complete"
    FAILED = "failed"


class ProbeHarnessConfig(BaseModel):
    """Frozen paths and cost controls for one probe run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library_path: Path
    manifest_path: Path
    workspace: Path
    checkpoint_path: Path
    snapshot_path: Path
    run_name: str = Field(min_length=1)
    max_cost_usd: float = Field(gt=0)
    minimum_call_reserve_usd: float = Field(gt=0)
    answer_max_output_tokens: int = Field(default=2_000, ge=1)


class ExperimentItem(BaseModel):
    """Server-minted Langfuse item for one frozen probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)


class ExperimentSetup(BaseModel):
    """Dataset and stable experiment identities used by all run spans."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    experiment_name: str = Field(min_length=1)
    ingest_item_id: str = Field(min_length=1)
    probe_items: list[ExperimentItem]

    def item_id_for(self, probe_id: str) -> str:
        """Return the validated item identity for ``probe_id``."""
        return next(item.item_id for item in self.probe_items if item.probe_id == probe_id)


class CoreFileSnapshot(BaseModel):
    """One core-memory file at the post-Dream boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    content: str
    modified_at: datetime | None


class PersonaMemorySnapshot(BaseModel):
    """Core-memory state for one synthetic persona."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_id: str
    files: list[CoreFileSnapshot]


class DreamSnapshot(BaseModel):
    """Cursor-derived Dream completion state persisted for resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    iterations: int = Field(ge=0)
    exhausted: bool
    stalled: bool


class MemorySnapshot(BaseModel):
    """Post-ingest memory state that prevents ingestion on resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    captured_at: datetime
    suite_version: str
    max_context_tokens: int
    ingested_turns: int = Field(ge=0)
    ingest_cost_usd: float = Field(ge=0)
    dream: DreamSnapshot
    personas: list[PersonaMemorySnapshot]


class ProbeRunRecord(BaseModel):
    """Predict output passed unchanged to the ticket-23 scoring seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: Probe
    answer: str
    assembled_context: str
    trace_id: str
    span_id: str
    started_at: datetime
    completed_at: datetime
    snapshot_captured_at: datetime


class ProbeScore(BaseModel):
    """Narrow score result produced by ticket 23."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str = Field(pattern=r"^memory_", min_length=1)
    value: float | bool
    data_type: Literal["NUMERIC", "BOOLEAN"]


ScoreFn = Callable[[ProbeRunRecord], ProbeScore]
ExperimentSetupFn = Callable[[WorldSpec, str], Awaitable[ExperimentSetup]]


class ProbeHarnessServices(BaseModel):
    """Runtime collaborators kept separate from serializable run config."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    provider: LLMProvider
    pricebook: PriceBook
    score_fn: ScoreFn
    experiment_setup: ExperimentSetupFn
    score_injector: L2ScoreInjector


class ProbeCheckpoint(BaseModel):
    """One append-only completion record used for process resume."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str
    status: ProbeItemStatus
    cost_usd: float = Field(ge=0)
    record: ProbeRunRecord | None = None
    score: ProbeScore | None = None
    error: str | None = None


class ProbeHarnessResult(BaseModel):
    """Complete or cost-capped view over durable checkpoint records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: HarnessStatus
    experiment_id: str
    completed_probe_ids: list[str]
    records: list[ProbeRunRecord]
    failures: list[ProbeCheckpoint]
    ingested_turns: int = Field(ge=0)
    spent_cost_usd: float = Field(ge=0)
