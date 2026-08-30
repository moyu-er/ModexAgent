"""Strict value models for rubric judge inputs and outcomes."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.judge.rubrics import RubricSet


class Verdict(StrEnum):
    """Closed judge verdict values used by callers and aggregation."""

    MET = "MET"
    UNMET = "UNMET"
    NA = "NA"
    CANNOT_ASSESS = "CANNOT_ASSESS"


type VerdictLiteral = Literal["MET", "UNMET", "NA", "CANNOT_ASSESS"]


class JudgeVerdictFlag(StrEnum):
    """Deterministic post-judge findings retained with one verdict."""

    CITATION_FABRICATED = "citation_fabricated"
    STALE = "stale"


class JudgeInput(BaseModel):
    """One candidate output and the rubric context used to judge it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_context: str
    rubric_set: RubricSet
    agent_output: str
    trace_id: str | None = None
    session_id: str | None = None


class JudgeVerdict(BaseModel):
    """One rubric-aligned verdict with evidence from the candidate output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str
    verdict: VerdictLiteral
    evidence: str
    flags: list[JudgeVerdictFlag] = Field(default_factory=list)


class JudgeProvenance(BaseModel):
    """Determinism and rubric identity recorded with a judge result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    judge_model: str
    rubric_version: str
    seed_applied: bool
    temperature: float = 0.0
    answer_model: str | None = None
    same_model_override: bool = False


class JudgeResult(BaseModel):
    """Parsed and aggregated outcome of one judge review."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[JudgeVerdict]
    summary: str
    weighted_score: float
    na_count: int
    parse_ok: bool
    raw_output: str
    provenance: JudgeProvenance


class JudgeConfig(BaseModel):
    """Stable request controls for a judge runner instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = 0
    max_output_tokens: int | None = None


class JudgeResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[JudgeVerdict]
    summary: str
