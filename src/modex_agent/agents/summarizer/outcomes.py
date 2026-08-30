from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from modex_agent.memory.hooks import LlmUsage


class CompactionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str
    usage: LlmUsage | None = None


class ConsolidationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    changed: bool
    usage: LlmUsage | None = None


__all__ = ["CompactionOutcome", "ConsolidationOutcome"]
