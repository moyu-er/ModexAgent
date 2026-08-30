"""Strict value models for judge calibration inputs, reports, and status."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.judge._models import Verdict


class ConfusionMatrix(BaseModel):
    """Binary judge/human counts; non-binary pairs are excluded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    met_met: int = 0
    met_unmet: int = 0
    unmet_met: int = 0
    unmet_unmet: int = 0


class KappaResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: float | None
    matrix: ConfusionMatrix


class SkewResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    fp: int
    fn: int
    ratio: float
    trigger: bool


class VerdictWithMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Verdict
    answer_length: int = Field(ge=0)


class BiasResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    short_met_rate: float
    long_met_rate: float
    long_short_gap_pp: float
    trigger: bool


class RetestResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agreement: float
    passes: bool


class DimensionCalibrationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    judge: list[Verdict]
    human: list[Verdict]


class CalibrationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimensions: list[DimensionCalibrationInput]
    retest_reviews: list[list[Verdict]]
    bias_items: list[VerdictWithMeta]


class DimensionCalibrationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kappa: KappaResult
    skew: SkewResult
    degenerate: bool
    passes: bool | None


class CalibrationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimensions: list[DimensionCalibrationResult]
    overall_kappa: KappaResult
    overall_skew: SkewResult
    retest: RetestResult
    na_rate: float
    bias: BiasResult
    passes: bool


class CalibrationStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    calibrated: bool
    checked_at: datetime | None


class CalibrationTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rubric_set: str
    judge_model: str


class CalibrationRunRecord(BaseModel):
    """An explicit calibration execution receipt; the sole promotion input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: CalibrationTarget
    report: CalibrationReport
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JudgeScoreComment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["judge"] = "judge"
    version: str
    report_source: Literal["llm_judge"] = "llm_judge"
    run_ref: str
    calibrated: bool
