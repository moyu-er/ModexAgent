"""Typed output contract for EvalRunner v2 tasks."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from modex_agent.core.emitter import StopReason


class WorldResult(BaseModel):
    """Observed result of one world-state assertion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    assertion: str
    passed: bool
    detail: str


class ToolStats(BaseModel):
    """Aggregate tool trajectory metrics for one eval item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int
    errors: int
    success_rate: float
    source: Literal["metrics", "messages"]


class TurnRecord(BaseModel):
    """Terminal result recorded for one eval turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_reason: StopReason
    error: str | None
    content: str


class EvalTaskOutput(BaseModel):
    """Serializable v2 task output consumed by evaluators and replay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output: str
    stop_reason: StopReason
    error: str | None
    world_results: list[WorldResult]
    tool_stats: ToolStats
    turns_executed: int
    stop_mismatches: list[str]
    turn_records: list[TurnRecord]

    def to_output_dict(self) -> dict[str, Any]:
        """Return the Langfuse/evaluator wire shape."""
        return self.model_dump(mode="json")


__all__ = ["EvalTaskOutput", "ToolStats", "TurnRecord", "WorldResult"]
