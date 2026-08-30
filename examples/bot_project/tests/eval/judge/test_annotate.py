from __future__ import annotations

from pathlib import Path

from bot.eval.judge._models import (
    JudgeProvenance,
    JudgeResult,
    JudgeVerdict,
    Verdict,
)
from bot.eval.judge.annotate import AnnotationRecord, B4CalibrationReceipt, app
from bot.eval.judge.calibration import (
    CalibrationInput,
    DimensionCalibrationInput,
    VerdictWithMeta,
)
from typer.testing import CliRunner

_RUNNER = CliRunner()


def _write_archive(path: Path, *, criteria: tuple[str, ...] = ("task_completion",)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    result = JudgeResult(
        verdicts=[
            JudgeVerdict(
                criterion=criterion,
                verdict="MET" if index % 2 == 0 else "UNMET",
                evidence=f"evidence-{criterion}",
            )
            for index, criterion in enumerate(criteria)
        ],
        summary="judge summary",
        weighted_score=1.0,
        na_count=0,
        parse_ok=True,
        raw_output="judge raw output",
        provenance=JudgeProvenance(
            judge_model="judge/test-model",
            rubric_version="1234abcd",
            seed_applied=True,
        ),
    )
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def _annotation_args(archive_dir: Path, output: Path) -> list[str]:
    return [
        "annotate",
        "--archive-dir",
        str(archive_dir),
        "--output",
        str(output),
        "--rubric-set",
        "general-agent",
    ]


def test_annotation_cli_round_trips_judge_and_human_verdicts(tmp_path: Path) -> None:
    # Given: one T18 archive with two rubric-aligned judge verdicts.
    archive_dir = tmp_path / "judge"
    output = tmp_path / "annotations.jsonl"
    _write_archive(
        archive_dir / "trace-1.json",
        criteria=("task_completion", "empirical_verification"),
    )

    # When: a human labels both dimensions.
    result = _RUNNER.invoke(
        app,
        _annotation_args(archive_dir, output),
        input="MET\nUNMET\n",
    )

    # Then: the frozen JSONL preserves one aligned judge/human pair per dimension.
    assert result.exit_code == 0
    records = [
        AnnotationRecord.model_validate_json(line)
        for line in output.read_text(encoding="utf-8").splitlines()
    ]
    assert [(record.trace_id, record.criterion) for record in records] == [
        ("trace-1", "task_completion"),
        ("trace-1", "empirical_verification"),
    ]
    assert [record.judge_verdict for record in records] == [Verdict.MET, Verdict.UNMET]
    assert [record.human_verdict for record in records] == [Verdict.MET, Verdict.UNMET]
    assert "trace-1" in result.output
    assert "task_completion" in result.output
    assert "Judge verdict: MET" in result.output


def test_annotation_cli_resumes_with_only_unlabelled_dimensions(tmp_path: Path) -> None:
    # Given: a two-dimension archive and an interrupted first session.
    archive_dir = tmp_path / "judge"
    output = tmp_path / "annotations.jsonl"
    _write_archive(
        archive_dir / "trace-1.json",
        criteria=("task_completion", "empirical_verification"),
    )
    interrupted = _RUNNER.invoke(
        app,
        _annotation_args(archive_dir, output),
        input="MET\n",
    )
    assert interrupted.exit_code != 0

    # When: the operator re-enters and supplies one remaining label.
    resumed = _RUNNER.invoke(
        app,
        _annotation_args(archive_dir, output),
        input="UNMET\n",
    )

    # Then: the completed dimension is skipped and only the remaining one is shown.
    assert resumed.exit_code == 0
    assert "task_completion" not in resumed.output
    assert resumed.output.count("empirical_verification") >= 1
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_annotation_cli_rejects_invalid_input_and_reshows_current_item(tmp_path: Path) -> None:
    # Given: one unlabelled rubric dimension.
    archive_dir = tmp_path / "judge"
    output = tmp_path / "annotations.jsonl"
    _write_archive(archive_dir / "trace-1.json")

    # When: the first human verdict is invalid and the second is valid.
    result = _RUNNER.invoke(
        app,
        _annotation_args(archive_dir, output),
        input="maybe\nNA\n",
    )

    # Then: the current item is shown again and only the valid verdict is persisted.
    assert result.exit_code == 0
    assert "Invalid verdict" in result.output
    assert result.output.count("task_completion") >= 2
    record = AnnotationRecord.model_validate_json(output.read_text(encoding="utf-8"))
    assert record.human_verdict is Verdict.NA


def test_annotation_cli_reports_empty_archive_without_creating_output(tmp_path: Path) -> None:
    # Given: an empty judge archive directory.
    archive_dir = tmp_path / "judge"
    archive_dir.mkdir()
    output = tmp_path / "annotations.jsonl"

    # When: annotation is dispatched.
    result = _RUNNER.invoke(app, _annotation_args(archive_dir, output))

    # Then: the CLI exits cleanly with an explicit empty report.
    assert result.exit_code == 0
    assert "No judge archives found" in result.output
    assert not output.exists()


def test_smoke_receipt_marks_kappa_and_confusion_matrix_pending(tmp_path: Path) -> None:
    # Given: a smoke judge archive and one partial human annotation.
    archive_dir = tmp_path / "judge"
    annotations = tmp_path / "annotations.jsonl"
    output = tmp_path / "b4_calibration.json"
    _write_archive(archive_dir / "trace-1.json")
    annotated = _RUNNER.invoke(
        app,
        _annotation_args(archive_dir, annotations),
        input="MET\n",
    )
    assert annotated.exit_code == 0

    # When: only the three-repeat smoke statistic is assembled.
    result = _RUNNER.invoke(
        app,
        [
            "receipt",
            "--experiment",
            "calibration-smoke-v1",
            "--rubric-set",
            "general-agent",
            "--judge-model",
            "judge/test-model",
            "--archive-dir",
            str(archive_dir),
            "--annotations",
            str(annotations),
            "--retest-repeats",
            "3",
            "--retest-agreement",
            "1.0",
            "--output",
            str(output),
        ],
    )

    # Then: the B4 receipt is useful but cannot imply unperformed calibration.
    assert result.exit_code == 0
    receipt = B4CalibrationReceipt.model_validate_json(output.read_text(encoding="utf-8"))
    assert receipt.sample_count == 1
    assert receipt.annotation_count == 1
    assert receipt.retest.repeats == 3
    assert receipt.retest.agreement == 1.0
    assert receipt.kappa.status == "pending"
    assert receipt.confusion_matrices.status == "pending"
    assert receipt.calibrated is False
    assert receipt.gray_flag is True


def test_full_receipt_uses_calibration_run_and_persists_status(tmp_path: Path) -> None:
    # Given: typed calibration input assembled after a completed human session.
    verdicts = [Verdict.MET, Verdict.UNMET] * 10
    calibration_input = CalibrationInput(
        dimensions=[
            DimensionCalibrationInput(
                name="task_completion",
                judge=verdicts,
                human=verdicts,
            )
        ],
        retest_reviews=[verdicts, verdicts, verdicts],
        bias_items=[
            VerdictWithMeta(verdict=verdict, answer_length=index + 1)
            for index, verdict in enumerate(verdicts)
        ],
    )
    input_path = tmp_path / "calibration_input.json"
    run_record = tmp_path / "calibration_run.json"
    status_dir = tmp_path / "status"
    input_path.write_text(calibration_input.model_dump_json(indent=2), encoding="utf-8")

    # When: the calibration entry and full receipt commands are dispatched.
    calibrated = _RUNNER.invoke(
        app,
        [
            "calibrate",
            "--input",
            str(input_path),
            "--rubric-set",
            "general-agent",
            "--judge-model",
            "judge/test-model",
            "--run-record",
            str(run_record),
            "--status-dir",
            str(status_dir),
        ],
    )
    archive_dir = tmp_path / "judge"
    _write_archive(archive_dir / "trace-1.json")
    output = tmp_path / "b4_calibration.json"
    receipt_result = _RUNNER.invoke(
        app,
        [
            "receipt",
            "--experiment",
            "calibration-final-v1",
            "--rubric-set",
            "general-agent",
            "--judge-model",
            "judge/test-model",
            "--archive-dir",
            str(archive_dir),
            "--retest-repeats",
            "3",
            "--retest-agreement",
            "1.0",
            "--calibration-run",
            str(run_record),
            "--output",
            str(output),
        ],
    )

    # Then: κ, confusion matrices, and the fail-closed status share one report.
    assert calibrated.exit_code == 0
    assert receipt_result.exit_code == 0
    receipt = B4CalibrationReceipt.model_validate_json(output.read_text(encoding="utf-8"))
    assert receipt.kappa.status == "complete"
    assert receipt.kappa.overall == 1.0
    assert receipt.confusion_matrices.status == "complete"
    assert receipt.confusion_matrices.overall is not None
    assert receipt.calibrated is True
    assert receipt.gray_flag is False
    assert len(list(status_dir.glob("*.json"))) == 1
