"""Typed contracts for deterministic memory evaluation metrics."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

type NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
type NonnegativeFloat = Annotated[float, Field(ge=0)]


class UtilizationClass(StrEnum):
    """Closed benefit labels for dual-arm probe answers."""

    BENEFICIAL = "beneficial"
    HARMFUL = "harmful"
    IGNORED = "ignored"
    NEUTRAL = "neutral"


class ProbeDeltaRecord(BaseModel):
    """One dual-arm probe answer's deterministic benefit label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_id: str = Field(min_length=1)
    label: UtilizationClass


class DistributionStats(BaseModel):
    """Minimum, arithmetic mean, and maximum latency in milliseconds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_ms: NonnegativeFloat
    mean_ms: NonnegativeFloat
    max_ms: NonnegativeFloat


class UtilizationDelta(BaseModel):
    """Counts of the four dual-arm utilization outcomes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    beneficial: NonnegativeInt = 0
    harmful: NonnegativeInt = 0
    ignored: NonnegativeInt = 0
    neutral: NonnegativeInt = 0


class MemoryMetrics(BaseModel):
    """Run-level deterministic memory indicators with group no-data flags."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compression_no_data: bool
    memory_compression_ratio: NonnegativeFloat | None
    memory_compression_monotonic: bool | None
    prefix_stable: bool | None
    write_no_data: bool
    memory_write_cost_usd: NonnegativeFloat
    read_no_data: bool
    memory_read_latency_ms: DistributionStats | None
    injection_retention: NonnegativeFloat | None
    utilization_no_data: bool
    utilization_delta: UtilizationDelta | None
