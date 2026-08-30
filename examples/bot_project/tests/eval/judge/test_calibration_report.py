from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from bot.eval.judge.calibration import (
    CalibrationInput,
    CalibrationRunRecord,
    CalibrationTarget,
    DimensionCalibrationInput,
    VerdictWithMeta,
    calibration_report,
    load_calibration_status,
    record_calibration_run,
)
from bot.eval.judge.runner import Verdict


def _balanced_verdicts() -> list[Verdict]:
    return [Verdict.MET, Verdict.UNMET] * 10


def _balanced_bias_items() -> list[VerdictWithMeta]:
    return [
        VerdictWithMeta(
            verdict=Verdict.MET if index % 2 == 0 else Verdict.UNMET,
            answer_length=index + 1,
        )
        for index in range(20)
    ]


def _passing_input() -> CalibrationInput:
    verdicts = _balanced_verdicts()
    return CalibrationInput(
        dimensions=[
            DimensionCalibrationInput(
                name="task_completion",
                judge=verdicts,
                human=verdicts,
            ),
            DimensionCalibrationInput(
                name="grounded_reporting",
                judge=verdicts,
                human=verdicts,
            ),
        ],
        retest_reviews=[verdicts, verdicts, verdicts],
        bias_items=_balanced_bias_items(),
    )


def test_calibration_report_assembles_full_passing_pipeline() -> None:
    # Given / When
    report = calibration_report(_passing_input())

    # Then
    assert report.passes is True
    assert report.overall_kappa.value == 1.0
    assert report.retest.agreement == 1.0
    assert report.na_rate == 0.0
    assert report.bias.long_short_gap_pp == 0.0
    assert report.overall_skew.trigger is False
    assert [dimension.name for dimension in report.dimensions] == [
        "task_completion",
        "grounded_reporting",
    ]
    assert all(dimension.kappa.matrix.met_met == 10 for dimension in report.dimensions)


def test_calibration_report_marks_degenerate_human_dimension_na() -> None:
    # Given
    verdicts = [Verdict.MET] * 20
    calibration_input = CalibrationInput(
        dimensions=[
            DimensionCalibrationInput(
                name="degenerate",
                judge=verdicts,
                human=verdicts,
            )
        ],
        retest_reviews=[verdicts, verdicts, verdicts],
        bias_items=[
            VerdictWithMeta(verdict=Verdict.MET, answer_length=index + 1)
            for index in range(20)
        ],
    )

    # When
    report = calibration_report(calibration_input)

    # Then
    assert report.dimensions[0].degenerate is True
    assert report.dimensions[0].passes is None
    assert report.overall_kappa.value is None
    assert report.passes is False


def test_calibration_report_fails_all_na_input() -> None:
    # Given
    verdicts = [Verdict.NA] * 20
    calibration_input = CalibrationInput(
        dimensions=[
            DimensionCalibrationInput(name="unknown", judge=verdicts, human=verdicts)
        ],
        retest_reviews=[verdicts, verdicts, verdicts],
        bias_items=[
            VerdictWithMeta(verdict=Verdict.NA, answer_length=index + 1)
            for index in range(20)
        ],
    )

    # When
    report = calibration_report(calibration_input)

    # Then
    assert report.na_rate == 1.0
    assert report.overall_kappa.value is None
    assert report.passes is False


def test_status_absence_and_calculation_do_not_auto_promote(tmp_path: Path) -> None:
    # Given / When
    report = calibration_report(_passing_input())
    status = load_calibration_status("general-agent", "judge/model", tmp_path)

    # Then
    assert report.passes is True
    assert status.calibrated is False
    assert status.checked_at is None
    assert list(tmp_path.iterdir()) == []


def test_explicit_calibration_run_writes_and_reads_status_round_trip(tmp_path: Path) -> None:
    # Given
    checked_at = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    run = CalibrationRunRecord(
        target=CalibrationTarget(rubric_set="general-agent", judge_model="judge/model"),
        report=calibration_report(_passing_input()),
        checked_at=checked_at,
    )

    # When
    written = record_calibration_run(run, tmp_path)
    loaded = load_calibration_status("general-agent", "judge/model", tmp_path)

    # Then
    assert written.calibrated is True
    assert loaded == written
    assert loaded.checked_at == checked_at
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_failed_explicit_run_revokes_stale_calibration(tmp_path: Path) -> None:
    # Given
    passing = CalibrationRunRecord(
        target=CalibrationTarget(rubric_set="general-agent", judge_model="judge-model"),
        report=calibration_report(_passing_input()),
        checked_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    record_calibration_run(passing, tmp_path)
    all_na = [Verdict.NA] * 20
    failing_input = CalibrationInput(
        dimensions=[DimensionCalibrationInput(name="unknown", judge=all_na, human=all_na)],
        retest_reviews=[all_na, all_na, all_na],
        bias_items=[
            VerdictWithMeta(verdict=Verdict.NA, answer_length=index + 1)
            for index in range(20)
        ],
    )
    failing = CalibrationRunRecord(
        target=passing.target,
        report=calibration_report(failing_input),
        checked_at=datetime(2026, 8, 22, tzinfo=UTC),
    )

    # When
    record_calibration_run(failing, tmp_path)
    loaded = load_calibration_status("general-agent", "judge-model", tmp_path)

    # Then
    assert loaded.calibrated is False
    assert loaded.checked_at == failing.checked_at


def test_corrupt_status_fails_closed_with_warning(tmp_path: Path) -> None:
    # Given
    (tmp_path / "general-agent@judge-model.json").write_text("not-json", encoding="utf-8")

    # When
    with pytest.warns(RuntimeWarning, match="corrupt calibration status"):
        status = load_calibration_status("general-agent", "judge-model", tmp_path)

    # Then
    assert status.calibrated is False
    assert status.checked_at is None
