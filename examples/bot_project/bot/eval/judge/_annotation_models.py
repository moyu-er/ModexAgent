"""Strict annotation and B4 calibration receipt value models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bot.eval.judge._calibration_models import CalibrationRunRecord, ConfusionMatrix
from bot.eval.judge._models import JudgeResult, Verdict


class ReceiptMetricStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"


class CalibrationReceiptMode(StrEnum):
    SMOKE = "smoke"
    FULL = "full"


class AnnotationRecord(BaseModel):
    """One immutable human verdict aligned with one archived judge verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    criterion: str
    rubric_description: str
    judge_verdict: Verdict
    judge_evidence: str
    judge_summary: str
    human_verdict: Verdict
    archive_path: str

    @field_validator("human_verdict")
    @classmethod
    def _human_verdict_excludes_cannot_assess(cls, verdict: Verdict) -> Verdict:
        if verdict is Verdict.CANNOT_ASSESS:
            msg = "human verdict must be MET, UNMET, or NA"
            raise ValueError(msg)
        return verdict


class JudgeArchiveEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    archive_path: Path
    result: JudgeResult


class RetestReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repeats: int = Field(ge=3)
    agreement: float = Field(ge=0.0, le=1.0)
    passes: bool


class NamedKappa(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str
    value: float | None


class KappaReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ReceiptMetricStatus
    overall: float | None = None
    dimensions: list[NamedKappa] = Field(default_factory=list)
    pending_reason: str | None = None


class NamedConfusionMatrix(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str
    matrix: ConfusionMatrix


class ConfusionMatricesReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ReceiptMetricStatus
    overall: ConfusionMatrix | None = None
    dimensions: list[NamedConfusionMatrix] = Field(default_factory=list)
    pending_reason: str | None = None


class CalibrationReceiptInput(BaseModel):
    """All typed artifacts needed to assemble one B4 receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment: str
    rubric_set: str
    judge_model: str
    archive_dir: Path
    annotations_path: Path | None
    retest_repeats: int = Field(ge=3)
    retest_agreement: float = Field(ge=0.0, le=1.0)
    judge_results: list[JudgeArchiveEntry]
    annotations: list[AnnotationRecord]
    calibration_run: CalibrationRunRecord | None = None


class B4CalibrationReceipt(BaseModel):
    """Stable smoke/full evidence shape consumed by the final B4 audit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["b4_calibration.v1"] = "b4_calibration.v1"
    mode: CalibrationReceiptMode
    generated_at: datetime
    experiment: str
    rubric_set: str
    judge_model: str
    judge_archive: str
    annotations: str | None
    sample_count: int
    annotation_count: int
    retest: RetestReceipt
    kappa: KappaReceipt
    confusion_matrices: ConfusionMatricesReceipt
    na_rate: float | None
    bias_gap_pp: float | None
    calibrated: bool
    gray_flag: bool
