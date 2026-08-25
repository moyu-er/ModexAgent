"""Judge calibration metrics, report assembly, and explicit status persistence."""

from __future__ import annotations

import warnings
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Final
from urllib.parse import quote

from pydantic import ValidationError

from bot.eval.judge._calibration_models import (
    BiasResult,
    CalibrationInput,
    CalibrationReport,
    CalibrationRunRecord,
    CalibrationStatus,
    ConfusionMatrix,
    DimensionCalibrationResult,
    KappaResult,
    RetestResult,
    SkewResult,
    VerdictWithMeta,
)
from bot.eval.judge._calibration_models import CalibrationTarget as CalibrationTarget
from bot.eval.judge._calibration_models import (
    DimensionCalibrationInput as DimensionCalibrationInput,
)
from bot.eval.judge._calibration_models import JudgeScoreComment as JudgeScoreComment
from bot.eval.judge._models import Verdict

DIMENSION_KAPPA_THRESHOLD: Final = 0.6
OVERALL_KAPPA_THRESHOLD: Final = 0.67
RETEST_MIN_REPEATS: Final = 3
RETEST_AGREEMENT_THRESHOLD: Final = 0.95
NA_RATE_THRESHOLD: Final = 0.05
DIRECTION_SKEW_RATIO_THRESHOLD: Final = 2.0
BIAS_GAP_PP_THRESHOLD: Final = 10.0
DEFAULT_CALIBRATION_DIR: Final = Path("evals/judge/calibration")


class CalibrationInputError(ValueError):
    """Raised when calibration vectors cannot describe comparable observations."""


def cohen_kappa(judge: list[Verdict], human: list[Verdict]) -> KappaResult:
    """Compute binary Cohen's kappa after excluding any non-binary pair."""
    if len(judge) != len(human):
        raise CalibrationInputError("judge and human verdict lists must have the same length")

    binary = {Verdict.MET, Verdict.UNMET}
    counts = Counter(
        (judge_verdict, human_verdict)
        for judge_verdict, human_verdict in zip(judge, human, strict=True)
        if judge_verdict in binary and human_verdict in binary
    )
    met_met = counts[(Verdict.MET, Verdict.MET)]
    met_unmet = counts[(Verdict.MET, Verdict.UNMET)]
    unmet_met = counts[(Verdict.UNMET, Verdict.MET)]
    unmet_unmet = counts[(Verdict.UNMET, Verdict.UNMET)]
    matrix = ConfusionMatrix(
        met_met=met_met,
        met_unmet=met_unmet,
        unmet_met=unmet_met,
        unmet_unmet=unmet_unmet,
    )
    total = met_met + met_unmet + unmet_met + unmet_unmet
    if total == 0:
        return KappaResult(value=None, matrix=matrix)
    judge_met = met_met + met_unmet
    judge_unmet = unmet_met + unmet_unmet
    human_met = met_met + unmet_met
    human_unmet = met_unmet + unmet_unmet
    expected_numerator = judge_met * human_met + judge_unmet * human_unmet
    denominator = total * total - expected_numerator
    if denominator == 0:
        return KappaResult(value=None, matrix=matrix)
    numerator = (met_met + unmet_unmet) * total - expected_numerator
    return KappaResult(value=float(Fraction(numerator, denominator)), matrix=matrix)


def na_rate(verdicts: list[Verdict]) -> float:
    """Return the NA plus CANNOT_ASSESS fraction, or zero for no observations."""
    if not verdicts:
        return 0.0
    unavailable = sum(
        verdict in {Verdict.NA, Verdict.CANNOT_ASSESS} for verdict in verdicts
    )
    return float(Fraction(unavailable, len(verdicts)))


def direction_skew(matrix: ConfusionMatrix) -> SkewResult:
    """Measure lenient FP versus strict FN imbalance."""
    fp = matrix.met_unmet
    fn = matrix.unmet_met
    if fp == fn:
        ratio = 1.0
    elif min(fp, fn) == 0:
        ratio = float("inf")
    else:
        ratio = max(fp, fn) / min(fp, fn)
    return SkewResult(
        fp=fp,
        fn=fn,
        ratio=ratio,
        trigger=fp != fn and ratio > DIRECTION_SKEW_RATIO_THRESHOLD,
    )


def bias_audit_long_short(judge: list[VerdictWithMeta]) -> BiasResult:
    """Compare judge-MET rates across answer-length median halves."""
    if len(judge) < 2:
        raise CalibrationInputError("bias audit requires at least 2 judged items")
    ordered = sorted(judge, key=lambda item: item.answer_length)
    midpoint = len(ordered) // 2
    short_items = ordered[:midpoint]
    long_items = ordered[midpoint:]
    short_rate = sum(item.verdict is Verdict.MET for item in short_items) / len(short_items)
    long_rate = sum(item.verdict is Verdict.MET for item in long_items) / len(long_items)
    gap_pp = abs(long_rate - short_rate) * 100.0
    return BiasResult(
        short_met_rate=short_rate,
        long_met_rate=long_rate,
        long_short_gap_pp=gap_pp,
        trigger=gap_pp >= BIAS_GAP_PP_THRESHOLD,
    )


def test_retest(reviews: list[list[Verdict]]) -> RetestResult:
    """Return the fraction of verdict slots unanimous across at least three repeats."""
    if len(reviews) < RETEST_MIN_REPEATS:
        raise CalibrationInputError("test-retest requires at least 3 repeats")
    slot_count = len(reviews[0])
    if any(len(review) != slot_count for review in reviews[1:]):
        raise CalibrationInputError("test-retest verdict lists must have the same length")
    if slot_count == 0:
        return RetestResult(agreement=0.0, passes=False)
    agreeing = sum(
        all(review[index] is reviews[0][index] for review in reviews[1:])
        for index in range(slot_count)
    )
    agreement = float(Fraction(agreeing, slot_count))
    return RetestResult(
        agreement=agreement,
        passes=agreement >= RETEST_AGREEMENT_THRESHOLD,
    )


def calibration_report(calibration: CalibrationInput) -> CalibrationReport:
    """Assemble all ticket-04 metrics and their joint production gate."""
    dimensions: list[DimensionCalibrationResult] = []
    overall_judge: list[Verdict] = []
    overall_human: list[Verdict] = []
    all_judge: list[Verdict] = []
    for dimension in calibration.dimensions:
        kappa = cohen_kappa(dimension.judge, dimension.human)
        labels = set(dimension.human)
        degenerate = labels == {Verdict.MET} or labels == {Verdict.UNMET}
        passes = None if degenerate else kappa.value is not None and kappa.value >= DIMENSION_KAPPA_THRESHOLD
        dimensions.append(
            DimensionCalibrationResult(
                name=dimension.name,
                kappa=kappa,
                skew=direction_skew(kappa.matrix),
                degenerate=degenerate,
                passes=passes,
            )
        )
        all_judge.extend(dimension.judge)
        if not degenerate:
            overall_judge.extend(dimension.judge)
            overall_human.extend(dimension.human)

    overall_kappa = cohen_kappa(overall_judge, overall_human)
    overall_skew = direction_skew(overall_kappa.matrix)
    retest = test_retest(calibration.retest_reviews)
    unavailable_rate = na_rate(all_judge)
    bias = bias_audit_long_short(calibration.bias_items)
    conclusive = [dimension for dimension in dimensions if dimension.passes is not None]
    passes = (
        bool(conclusive)
        and all(dimension.passes for dimension in conclusive)
        and overall_kappa.value is not None
        and overall_kappa.value >= OVERALL_KAPPA_THRESHOLD
        and retest.passes
        and unavailable_rate < NA_RATE_THRESHOLD
        and not any(dimension.skew.trigger for dimension in dimensions)
        and not overall_skew.trigger
        and not bias.trigger
    )
    return CalibrationReport(
        dimensions=dimensions,
        overall_kappa=overall_kappa,
        overall_skew=overall_skew,
        retest=retest,
        na_rate=unavailable_rate,
        bias=bias,
        passes=passes,
    )


def load_calibration_status(
    rubric_set: str,
    judge_model: str,
    directory: Path,
) -> CalibrationStatus:
    """Load calibration state, failing closed for absence or corrupt content."""
    path = _calibration_status_path(rubric_set, judge_model, directory)
    try:
        return CalibrationStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CalibrationStatus(calibrated=False, checked_at=None)
    except (OSError, UnicodeError, ValidationError) as error:
        warnings.warn(
            f"corrupt calibration status at {path}: {error}",
            RuntimeWarning,
            stacklevel=2,
        )
        return CalibrationStatus(calibrated=False, checked_at=None)


def record_calibration_run(
    run: CalibrationRunRecord,
    directory: Path,
) -> CalibrationStatus:
    """Persist the result of an explicit calibration run; no other path promotes."""
    status = CalibrationStatus(
        calibrated=run.report.passes,
        checked_at=run.checked_at,
    )
    path = _calibration_status_path(
        run.target.rubric_set,
        run.target.judge_model,
        directory,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(status.model_dump_json(indent=2), encoding="utf-8")
    return status


def _calibration_status_path(rubric_set: str, judge_model: str, directory: Path) -> Path:
    safe_rubric = quote(rubric_set, safe="")
    safe_model = quote(judge_model, safe="")
    return directory / f"{safe_rubric}@{safe_model}.json"
