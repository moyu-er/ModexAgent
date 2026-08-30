"""Resumable human annotation and B4 calibration evidence CLI."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final, assert_never

import typer

from bot.eval.judge._annotation_models import (
    AnnotationRecord,
    B4CalibrationReceipt,
    CalibrationReceiptInput,
    CalibrationReceiptMode,
    ConfusionMatricesReceipt,
    JudgeArchiveEntry,
    KappaReceipt,
    NamedConfusionMatrix,
    NamedKappa,
    ReceiptMetricStatus,
    RetestReceipt,
)
from bot.eval.judge._calibration_models import (
    CalibrationInput,
    CalibrationRunRecord,
    CalibrationTarget,
)
from bot.eval.judge._models import JudgeResult, Verdict
from bot.eval.judge.calibration import (
    DEFAULT_CALIBRATION_DIR,
    RETEST_AGREEMENT_THRESHOLD,
    calibration_report,
    record_calibration_run,
)
from bot.eval.judge.rubrics import load_rubric_set

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

_PENDING_REASON: Final = "human annotation and full calibration have not been dispatched"


def _load_judge_archives(archive_dir: Path) -> list[JudgeArchiveEntry]:
    entries: list[JudgeArchiveEntry] = []
    for path in sorted(archive_dir.glob("*.json")):
        entries.append(
            JudgeArchiveEntry(
                trace_id=path.stem,
                archive_path=path,
                result=JudgeResult.model_validate_json(path.read_text(encoding="utf-8")),
            )
        )
    return entries


def _load_annotations(path: Path | None) -> list[AnnotationRecord]:
    if path is None or not path.exists():
        return []
    return [
        AnnotationRecord.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _append_annotation(path: Path, record: AnnotationRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(record.model_dump_json())
        stream.write("\n")


def _render_item(entry: JudgeArchiveEntry, criterion: str, description: str) -> None:
    verdict = next(item for item in entry.result.verdicts if item.criterion == criterion)
    typer.echo(f"Item context: trace_id={entry.trace_id} archive={entry.archive_path.as_posix()}")
    typer.echo(f"Judge summary: {entry.result.summary}")
    typer.echo(f"Rubric: {criterion} — {description}")
    typer.echo(f"Judge verdict: {verdict.verdict}")
    typer.echo(f"Judge evidence: {verdict.evidence}")


def _prompt_human_verdict(
    entry: JudgeArchiveEntry,
    criterion: str,
    description: str,
) -> Verdict:
    while True:
        _render_item(entry, criterion, description)
        raw = typer.prompt("Human verdict [MET/UNMET/NA]").strip().upper()
        try:
            verdict = Verdict(raw)
        except ValueError:
            typer.echo("Invalid verdict; enter MET, UNMET, or NA.", err=True)
            continue
        match verdict:
            case Verdict.MET | Verdict.UNMET | Verdict.NA:
                return verdict
            case Verdict.CANNOT_ASSESS:
                typer.echo("Invalid verdict; enter MET, UNMET, or NA.", err=True)
            case unreachable:
                assert_never(unreachable)


@app.command()
def annotate(
    archive_dir: Annotated[Path, typer.Option("--archive-dir", help="T18 judge archive directory.")],
    output: Annotated[Path, typer.Option("--output", help="Append-only human annotation JSONL.")],
    rubric_set: Annotated[str, typer.Option("--rubric-set", help="Central rubric set name.")] = "general-agent",
) -> None:
    """Label every unlabelled trace/rubric pair and resume from existing JSONL."""
    entries = _load_judge_archives(archive_dir)
    if not entries:
        typer.echo(f"No judge archives found in {archive_dir}")
        return
    rubrics = {rubric.criterion: rubric.description for rubric in load_rubric_set(rubric_set).rubrics}
    completed = {(record.trace_id, record.criterion) for record in _load_annotations(output)}
    added = 0
    for entry in entries:
        for judge_verdict in entry.result.verdicts:
            key = (entry.trace_id, judge_verdict.criterion)
            if key in completed:
                continue
            description = rubrics[judge_verdict.criterion]
            human_verdict = _prompt_human_verdict(entry, judge_verdict.criterion, description)
            _append_annotation(
                output,
                AnnotationRecord(
                    trace_id=entry.trace_id,
                    criterion=judge_verdict.criterion,
                    rubric_description=description,
                    judge_verdict=Verdict(judge_verdict.verdict),
                    judge_evidence=judge_verdict.evidence,
                    judge_summary=entry.result.summary,
                    human_verdict=human_verdict,
                    archive_path=entry.archive_path.as_posix(),
                ),
            )
            completed.add(key)
            added += 1
    typer.echo(f"Annotation complete: added={added} total={len(completed)} output={output}")


def assemble_calibration_receipt(receipt_input: CalibrationReceiptInput) -> B4CalibrationReceipt:
    """Assemble one stable smoke/full B4 receipt without promoting pending data."""
    run = receipt_input.calibration_run
    if run is None:
        retest_agreement = receipt_input.retest_agreement
        retest_passes = retest_agreement >= RETEST_AGREEMENT_THRESHOLD
        kappa = KappaReceipt(status=ReceiptMetricStatus.PENDING, pending_reason=_PENDING_REASON)
        confusion = ConfusionMatricesReceipt(
            status=ReceiptMetricStatus.PENDING,
            pending_reason=_PENDING_REASON,
        )
        mode = CalibrationReceiptMode.SMOKE
        na_rate = None
        bias_gap_pp = None
        calibrated = False
    else:
        report = run.report
        retest_agreement = report.retest.agreement
        retest_passes = report.retest.passes
        kappa = KappaReceipt(
            status=ReceiptMetricStatus.COMPLETE,
            overall=report.overall_kappa.value,
            dimensions=[
                NamedKappa(criterion=dimension.name, value=dimension.kappa.value)
                for dimension in report.dimensions
            ],
        )
        confusion = ConfusionMatricesReceipt(
            status=ReceiptMetricStatus.COMPLETE,
            overall=report.overall_kappa.matrix,
            dimensions=[
                NamedConfusionMatrix(criterion=dimension.name, matrix=dimension.kappa.matrix)
                for dimension in report.dimensions
            ],
        )
        mode = CalibrationReceiptMode.FULL
        na_rate = report.na_rate
        bias_gap_pp = report.bias.long_short_gap_pp
        calibrated = report.passes
    return B4CalibrationReceipt(
        mode=mode,
        generated_at=datetime.now(UTC),
        experiment=receipt_input.experiment,
        rubric_set=receipt_input.rubric_set,
        judge_model=receipt_input.judge_model,
        judge_archive=receipt_input.archive_dir.as_posix(),
        annotations=(
            receipt_input.annotations_path.as_posix()
            if receipt_input.annotations_path is not None
            else None
        ),
        sample_count=len(receipt_input.judge_results),
        annotation_count=len(receipt_input.annotations),
        retest=RetestReceipt(
            repeats=receipt_input.retest_repeats,
            agreement=retest_agreement,
            passes=retest_passes,
        ),
        kappa=kappa,
        confusion_matrices=confusion,
        na_rate=na_rate,
        bias_gap_pp=bias_gap_pp,
        calibrated=calibrated,
        gray_flag=not calibrated,
    )


def _write_model(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@app.command()
def calibrate(
    input_path: Annotated[Path, typer.Option("--input", help="Typed CalibrationInput JSON.")],
    rubric_set: Annotated[str, typer.Option("--rubric-set")],
    judge_model: Annotated[str, typer.Option("--judge-model")],
    run_record: Annotated[Path, typer.Option("--run-record")],
    status_dir: Annotated[Path, typer.Option("--status-dir")] = DEFAULT_CALIBRATION_DIR,
) -> None:
    """Compute T19 metrics, persist the sole calibration status, and freeze a run record."""
    calibration_input = CalibrationInput.model_validate_json(input_path.read_text(encoding="utf-8"))
    run = CalibrationRunRecord(
        target=CalibrationTarget(rubric_set=rubric_set, judge_model=judge_model),
        report=calibration_report(calibration_input),
    )
    _write_model(run_record, run.model_dump_json(indent=2))
    status = record_calibration_run(run, status_dir)
    typer.echo(f"Calibration recorded: calibrated={status.calibrated} run_record={run_record}")


@app.command()
def receipt(
    experiment: Annotated[str, typer.Option("--experiment")],
    rubric_set: Annotated[str, typer.Option("--rubric-set")],
    judge_model: Annotated[str, typer.Option("--judge-model")],
    archive_dir: Annotated[Path, typer.Option("--archive-dir")],
    retest_repeats: Annotated[int, typer.Option("--retest-repeats", min=3)],
    retest_agreement: Annotated[float, typer.Option("--retest-agreement", min=0.0, max=1.0)],
    output: Annotated[Path, typer.Option("--output")],
    annotations: Annotated[Path | None, typer.Option("--annotations")] = None,
    calibration_run: Annotated[Path | None, typer.Option("--calibration-run")] = None,
) -> None:
    """Write the stable B4 evidence shape for smoke or completed calibration."""
    run = (
        CalibrationRunRecord.model_validate_json(calibration_run.read_text(encoding="utf-8"))
        if calibration_run is not None
        else None
    )
    if run is not None and (
        run.target.rubric_set != rubric_set or run.target.judge_model != judge_model
    ):
        raise typer.BadParameter("calibration run target does not match rubric/model options")
    receipt_value = assemble_calibration_receipt(
        CalibrationReceiptInput(
            experiment=experiment,
            rubric_set=rubric_set,
            judge_model=judge_model,
            archive_dir=archive_dir,
            annotations_path=annotations,
            retest_repeats=retest_repeats,
            retest_agreement=retest_agreement,
            judge_results=_load_judge_archives(archive_dir),
            annotations=_load_annotations(annotations),
            calibration_run=run,
        )
    )
    _write_model(output, receipt_value.model_dump_json(indent=2))
    typer.echo(f"B4 calibration receipt written: {output}")


if __name__ == "__main__":
    app()
