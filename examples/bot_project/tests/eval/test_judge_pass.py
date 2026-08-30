from __future__ import annotations

import json
from pathlib import Path

import pytest
from bot.eval.judge import calibration
from bot.eval.judge.runner import JudgeRunner, Verdict
from bot.eval.judge_pass import (
    JudgePass,
    JudgePassConfig,
    JudgePassResources,
)

from tests.eval.judge_pass_fakes import (
    RecordingInjector,
    ScriptedCurator,
    ScriptedJudgeProvider,
    ScriptedObservationClient,
    experiment,
    observation,
    verdict_json,
)


async def test_judge_pass_injects_first_repeat_and_archives_full_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: two candidate traces, one with an intentionally empty agent output.
    monkeypatch.chdir(tmp_path)
    provider = ScriptedJudgeProvider(
        [verdict_json("first"), verdict_json("first"), verdict_json("second"), verdict_json("second")]
    )
    injector = RecordingInjector()
    lines: list[str] = []
    judge_pass = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator(
                {
                    "trace-1": {"trace_id": "trace-1", "input": "task one", "output": "answer one"},
                    "trace-2": {"trace_id": "trace-2", "input": "task two", "output": ""},
                }
            ),
            observation_client=ScriptedObservationClient(
                [[observation("trace-1"), observation("nested", parent_id="parent")], [observation("trace-2")]]
            ),
            runner=JudgeRunner(provider),
            injector=injector,
            emit=lines.append,
        )
    )
    config = JudgePassConfig(
        experiment="baseline-v1",
        repeats=2,
    )

    # When: the existing trajectories are judged twice without executing an agent.
    report = await judge_pass.run(config, experiment())

    # Then: first-repeat scores are injected once per trace with exact provenance.
    assert report.judged_count == 2
    assert provider.calls == 4
    assert [trace_id for trace_id, _ in injector.batches] == ["trace-1", "trace-2"]
    first_scores = injector.batches[0][1]
    assert [score.name for score in first_scores] == [
        "judge_rubric_overall",
        "judge_task_completion",
        "judge_empirical_verification",
        "judge_instruction_following",
        "judge_grounded_reporting",
        "judge_efficiency",
    ]
    assert all(score.data_type == "NUMERIC" for score in first_scores)
    assert [score.value for score in first_scores] == [0.65, 1.0, 0.0, 1.0, 0.0, 1.0]
    comments = {score.comment for score in first_scores}
    assert len(comments) == 1
    comment = json.loads(comments.pop() or "")
    assert comment == {
        "scorer": "judge",
        "version": f"judge.v1+{report.rubric_version}",
        "report_source": "llm_judge",
        "run_ref": "evals/runs/judge/baseline-v1",
        "calibrated": False,
    }

    # Then: each idempotent trace path contains the complete first verdict result.
    run_dir = tmp_path / "evals" / "runs" / "judge" / "baseline-v1"
    archived = json.loads((run_dir / "trace-1.json").read_text(encoding="utf-8"))
    assert archived["summary"] == "Scripted summary."
    assert archived["verdicts"][0]["evidence"] == "first"
    assert (run_dir / "trace-2.json").is_file()
    assert any("trace_id=trace-1" in line and "na_count=1" in line for line in lines)
    assert any("judged=2" in line and "mean_score=" in line for line in lines)


async def test_judge_pass_reports_empty_experiment_without_calls(tmp_path: Path) -> None:
    # Given: an experiment window containing no candidate root traces.
    provider = ScriptedJudgeProvider([])
    injector = RecordingInjector()
    lines: list[str] = []
    judge_pass = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator({}),
            observation_client=ScriptedObservationClient([[]]),
            runner=JudgeRunner(provider),
            injector=injector,
            emit=lines.append,
        )
    )

    # When: the standalone pass runs.
    report = await judge_pass.run(
        JudgePassConfig(experiment="baseline-v1", archive_root=tmp_path),
        experiment(),
    )

    # Then: it exits cleanly with an explicit empty report.
    assert report.judged_count == 0
    assert provider.calls == 0
    assert injector.batches == []
    assert lines == ["no traces for experiment 'baseline-v1'; judged=0"]


async def test_judge_pass_skips_fetch_failure_and_reports_repeat_agreement(
    tmp_path: Path,
) -> None:
    # Given: one unreadable trace and one trace whose repeated verdict changes.
    provider = ScriptedJudgeProvider(
        [verdict_json("first"), verdict_json("second", completion="UNMET")]
    )
    injector = RecordingInjector()
    lines: list[str] = []
    judge_pass = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator(
                {
                    "trace-bad": None,
                    "trace-good": {"trace_id": "trace-good", "input": "task", "output": "answer"},
                }
            ),
            observation_client=ScriptedObservationClient(
                [[observation("trace-bad"), observation("trace-good")]]
            ),
            runner=JudgeRunner(provider),
            injector=injector,
            emit=lines.append,
        )
    )

    # When: two repeat reviews are requested.
    report = await judge_pass.run(
        JudgePassConfig(
            experiment="baseline-v1",
            repeats=2,
            archive_root=tmp_path,
        ),
        experiment(),
    )

    # Then: the bad trace warns, the good trace continues, and agreement is measurable.
    assert report.judged_count == 1
    assert report.agreement_rate == pytest.approx(0.5)
    assert [trace_id for trace_id, _ in injector.batches] == ["trace-good"]
    assert any("WARNING trace_id=trace-bad" in line for line in lines)
    assert any("agreement=50.0%" in line for line in lines)


async def test_judge_pass_rerun_overwrites_trace_archive(tmp_path: Path) -> None:
    # Given: the same trace was judged once with stale evidence.
    config = JudgePassConfig(experiment="baseline-v1", archive_root=tmp_path)
    observation_pages = [[observation("trace-1")]]
    trace_io = {"trace-1": {"trace_id": "trace-1", "input": "task", "output": "answer"}}
    first = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator(trace_io),
            observation_client=ScriptedObservationClient(observation_pages),
            runner=JudgeRunner(ScriptedJudgeProvider([verdict_json("stale")])),
            injector=RecordingInjector(),
            emit=lambda _line: None,
        )
    )
    await first.run(config, experiment())

    # When: the experiment is re-judged with fresh evidence.
    second = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator(trace_io),
            observation_client=ScriptedObservationClient(observation_pages),
            runner=JudgeRunner(ScriptedJudgeProvider([verdict_json("fresh")])),
            injector=RecordingInjector(),
            emit=lambda _line: None,
        )
    )
    await second.run(config, experiment())

    # Then: the deterministic trace file contains only the fresh result.
    archived = json.loads((tmp_path / "baseline-v1" / "trace-1.json").read_text(encoding="utf-8"))
    assert archived["verdicts"][0]["evidence"] == "fresh"


async def test_judge_pass_limit_caps_candidate_traces(tmp_path: Path) -> None:
    # Given: two candidate traces and one available judge response.
    provider = ScriptedJudgeProvider([verdict_json("first")])
    injector = RecordingInjector()
    judge_pass = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator(
                {
                    "trace-1": {"trace_id": "trace-1", "input": "task", "output": "one"},
                    "trace-2": {"trace_id": "trace-2", "input": "task", "output": "two"},
                }
            ),
            observation_client=ScriptedObservationClient(
                [[observation("trace-1"), observation("trace-2")]]
            ),
            runner=JudgeRunner(provider),
            injector=injector,
            emit=lambda _line: None,
        )
    )

    # When: the pass is capped at one trace.
    report = await judge_pass.run(
        JudgePassConfig(
            experiment="baseline-v1",
            limit=1,
            archive_root=tmp_path,
        ),
        experiment(),
    )

    # Then: only the first candidate is reviewed, archived, and injected.
    assert report.judged_count == 1
    assert provider.calls == 1
    assert [trace_id for trace_id, _ in injector.batches] == ["trace-1"]
    assert not (tmp_path / "baseline-v1" / "trace-2.json").exists()


async def test_judge_pass_reads_explicit_calibration_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: an explicit successful calibration exists for this rubric/model pair.
    monkeypatch.chdir(tmp_path)
    verdicts = [Verdict.MET, Verdict.UNMET] * 10
    report = calibration.calibration_report(
        calibration.CalibrationInput(
            dimensions=[
                calibration.DimensionCalibrationInput(
                    name="task_completion",
                    judge=verdicts,
                    human=verdicts,
                )
            ],
            retest_reviews=[verdicts, verdicts, verdicts],
            bias_items=[
                calibration.VerdictWithMeta(
                    verdict=Verdict.MET if index % 2 == 0 else Verdict.UNMET,
                    answer_length=index + 1,
                )
                for index in range(20)
            ],
        )
    )
    calibration.record_calibration_run(
        calibration.CalibrationRunRecord(
            target=calibration.CalibrationTarget(
                rubric_set="general-agent",
                judge_model="judge/test-model",
            ),
            report=report,
        ),
        Path("evals/judge/calibration"),
    )
    injector = RecordingInjector()
    judge_pass = JudgePass(
        JudgePassResources(
            curator=ScriptedCurator(
                {"trace-1": {"trace_id": "trace-1", "input": "task", "output": "answer"}}
            ),
            observation_client=ScriptedObservationClient([[observation("trace-1")]]),
            runner=JudgeRunner(ScriptedJudgeProvider([verdict_json("evidence")])),
            injector=injector,
            emit=lambda _line: None,
        )
    )

    # When
    await judge_pass.run(JudgePassConfig(experiment="baseline-v1"), experiment())

    # Then
    comment = json.loads(injector.batches[0][1][0].comment or "")
    assert comment["calibrated"] is True
