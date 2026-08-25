from __future__ import annotations

from collections.abc import Sequence

from evals.sentinel.tasks import SentinelArm
from pydantic import BaseModel, ConfigDict

from bot.eval.sentinel.results import AssertionResult, SentinelTaskResult, SentinelTaskStatus


class SentinelTaskReportRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    arm: SentinelArm
    status: SentinelTaskStatus
    world_assertions: tuple[AssertionResult, ...]
    memory_assertions: tuple[AssertionResult, ...]
    error: str | None


class SentinelArmReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    arm: SentinelArm
    total_tasks: int
    succeeded_tasks: int
    failed_tasks: int
    success_rate: float | None
    all_failed: bool


class SentinelArmDifference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success_count_delta: int
    success_rate_delta: float | None


class SentinelDifferenceReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[SentinelTaskReportRow, ...]
    memory: SentinelArmReport
    nomemory: SentinelArmReport
    difference: SentinelArmDifference


def generate_difference_report(
    task_results: Sequence[SentinelTaskResult],
) -> SentinelDifferenceReport:
    """Build a truthful two-arm report without applying a directional gate."""
    rows = tuple(
        SentinelTaskReportRow(
            task_id=result.task_id,
            arm=result.arm,
            status=result.status,
            world_assertions=result.world_assertions,
            memory_assertions=result.memory_assertions,
            error=result.error,
        )
        for result in task_results
    )
    memory = _arm_report(task_results, SentinelArm.MEMORY)
    nomemory = _arm_report(task_results, SentinelArm.NOMEMORY)
    rate_delta = (
        None
        if memory.success_rate is None or nomemory.success_rate is None
        else memory.success_rate - nomemory.success_rate
    )
    return SentinelDifferenceReport(
        rows=rows,
        memory=memory,
        nomemory=nomemory,
        difference=SentinelArmDifference(
            success_count_delta=memory.succeeded_tasks - nomemory.succeeded_tasks,
            success_rate_delta=rate_delta,
        ),
    )


def _arm_report(
    task_results: Sequence[SentinelTaskResult],
    arm: SentinelArm,
) -> SentinelArmReport:
    selected = tuple(result for result in task_results if result.arm is arm)
    succeeded = sum(result.status.is_success for result in selected)
    total = len(selected)
    return SentinelArmReport(
        arm=arm,
        total_tasks=total,
        succeeded_tasks=succeeded,
        failed_tasks=total - succeeded,
        success_rate=succeeded / total if total else None,
        all_failed=total > 0 and succeeded == 0,
    )


__all__ = [
    "SentinelArmDifference",
    "SentinelArmReport",
    "SentinelDifferenceReport",
    "SentinelTaskReportRow",
    "generate_difference_report",
]
