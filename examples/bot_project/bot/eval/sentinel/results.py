from __future__ import annotations

from enum import StrEnum

from evals.sentinel.tasks import SentinelArm
from pydantic import BaseModel, ConfigDict, Field


class SentinelTaskStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    ERROR = "error"
    NO_TEST = "no_test"
    INSTALL_FAILED = "install_failed"

    @property
    def is_success(self) -> bool:
        return self is SentinelTaskStatus.SUCCESS


class AssertionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    assertion_id: str = Field(min_length=1)
    passed: bool
    details: str | None = None


class SentinelTaskObservation(BaseModel):
    """Execution-plane output before the orchestrator adds task identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: SentinelTaskStatus
    world_assertions: tuple[AssertionResult, ...]
    memory_assertions: tuple[AssertionResult, ...]
    error: str | None = None


class SentinelTaskResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    arm: SentinelArm
    status: SentinelTaskStatus
    world_assertions: tuple[AssertionResult, ...]
    memory_assertions: tuple[AssertionResult, ...]
    error: str | None = None


__all__ = [
    "AssertionResult",
    "SentinelTaskObservation",
    "SentinelTaskResult",
    "SentinelTaskStatus",
]
