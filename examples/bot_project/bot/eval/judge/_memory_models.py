"""Typed boundaries for memory-probe judging."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.judge._models import JudgeVerdictFlag, VerdictLiteral
from bot.eval.probes.schema import ProbeType

_DEFAULT_RUBRIC_SET: Final = "memory-probe"


class KnowledgeUpdateTier(StrEnum):
    """The three ticket-12 knowledge-update outcomes."""

    CURRENT = "current"
    STALE = "stale"
    NEITHER = "neither"


class MemoryTruth(BaseModel):
    """Programmatic truth supplied with a probe answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_answers: list[str]
    stale_answers: list[str] = Field(default_factory=list)
    forbidden_answers: list[str] = Field(default_factory=list)


class MemoryJudgeInput(BaseModel):
    """The ticket-11 answer package consumed by the specialized judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_type: ProbeType
    question: str
    truth: MemoryTruth
    candidate_answer: str
    injected_context: str
    answer_model: str
    trace_id: str | None = None
    session_id: str | None = None


class MemoryJudgeSettings(BaseModel):
    """Stable policy controls, including the explicit CLI override seam."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_same_model: bool = False
    rubric_set_name: str = _DEFAULT_RUBRIC_SET


class KnowledgeUpdateDecision(BaseModel):
    """Deterministic current/stale/neither classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: KnowledgeUpdateTier
    verdict: VerdictLiteral
    flags: list[JudgeVerdictFlag] = Field(default_factory=list)


class SameModelJudgeError(RuntimeError):
    """The independent judge model is identical to the answer model."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"judge model {model!r} equals the answer model; "
            "use --allow-same-model only for an audited override"
        )
