from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bot.eval import cli as eval_cli
from bot.eval import judge_cli
from bot.eval.judge_pass import ExperimentWindow, JudgePassConfig, JudgePassEnvironment
from typer.testing import CliRunner

from modex_agent.core.provider import LLMProvider
from modex_agent.runtime.models import JsonValue


def test_judge_requires_independent_model_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: Langfuse is configured but the independent judge model is absent.
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.delenv("JUDGE_MODEL", raising=False)

    # When: the standalone judge command is invoked.
    result = CliRunner().invoke(
        eval_cli.app,
        ["judge", "--experiment", "baseline-v1"],
    )

    # Then: the CLI names the missing environment variable and exits non-zero.
    assert result.exit_code == 1
    assert "JUDGE_MODEL" in result.output


def test_judge_forwards_repeat_and_archive_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: a fake command boundary that records the parsed judge options.
    captured: list[JudgePassConfig] = []
    monkeypatch.setattr(judge_cli, "_execute_judge_cli", captured.append)

    # When: all optional selection and repeat controls are supplied.
    result = CliRunner().invoke(
        eval_cli.app,
        [
            "judge",
            "--experiment",
            "baseline-v1",
            "--rubric-set",
            "general-agent",
            "--dataset",
            "math-qa",
            "--limit",
            "3",
            "--repeats",
            "2",
            "--archive-root",
            str(tmp_path),
        ],
    )

    # Then: the thin command passes a typed config to the execution boundary.
    assert result.exit_code == 0, result.output
    assert captured == [
        JudgePassConfig(
            experiment="baseline-v1",
            rubric_set="general-agent",
            dataset="math-qa",
            limit=3,
            repeats=2,
            archive_root=tmp_path,
        )
    ]


def test_judge_help_documents_first_repeat_injection() -> None:
    # Given: the eval CLI application.
    # When: judge-specific help is requested.
    result = CliRunner().invoke(eval_cli.app, ["judge", "--help"])

    # Then: repeat execution and first-result injection are explicit.
    assert result.exit_code == 0, result.output
    assert "first" in result.output.lower()
    assert "repeat" in result.output.lower()


def test_judge_resolves_experiment_window_and_runs_typed_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: one matching v4 experiment and fully mocked external boundaries.
    captured_fetch: list[tuple[str, dict[str, str], str | None]] = []
    captured_experiments: list[ExperimentWindow] = []
    provider = MagicMock(spec=LLMProvider)
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(judge_cli, "build_judge_provider_from_env", lambda: provider)

    def fetch_experiments(
        *,
        host: str,
        headers: dict[str, str],
        dataset_id: str | None,
    ) -> list[dict[str, JsonValue]]:
        captured_fetch.append((host, headers, dataset_id))
        return [
            {
                "id": "exp-1",
                "name": "baseline-v1",
                "datasetId": "dataset-1",
                "startTime": "2026-08-20T00:00:00Z",
                "endTime": "2026-08-21T00:00:00Z",
                "itemCount": 2,
            }
        ]

    async def run_from_env(
        _config: JudgePassConfig,
        experiment: ExperimentWindow,
        _environment: JudgePassEnvironment,
    ) -> None:
        captured_experiments.append(experiment)

    monkeypatch.setattr(eval_cli, "_fetch_experiments", fetch_experiments)
    monkeypatch.setattr(
        "bot.eval.judge_pass.run_judge_pass_from_env",
        run_from_env,
        raising=False,
    )

    # When: judge is invoked without a dataset disambiguator.
    result = CliRunner().invoke(
        eval_cli.app,
        [
            "judge",
            "--experiment",
            "baseline-v1",
            "--archive-root",
            str(tmp_path),
        ],
    )

    # Then: all experiments are queried and the exact window reaches the pass.
    assert result.exit_code == 0, result.output
    assert captured_fetch == [
        (
            "http://localhost:3000",
            {"Authorization": "Basic cHVibGljOnNlY3JldA=="},
            None,
        )
    ]
    [experiment] = captured_experiments
    assert experiment.name == "baseline-v1"
    assert experiment.start_time == datetime(2026, 8, 20, tzinfo=UTC)
    assert experiment.end_time == datetime(2026, 8, 21, tzinfo=UTC)


def test_judge_reports_missing_experiment_without_judge_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: judge configuration exists but the experiments API has no match.
    provider = MagicMock(spec=LLMProvider)
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(judge_cli, "build_judge_provider_from_env", lambda: provider)
    monkeypatch.setattr(eval_cli, "_fetch_experiments", lambda **_kwargs: [])
    run_from_env = MagicMock()
    monkeypatch.setattr(
        "bot.eval.judge_pass.run_judge_pass_from_env",
        run_from_env,
        raising=False,
    )

    # When: the unknown experiment is requested.
    result = CliRunner().invoke(
        eval_cli.app,
        ["judge", "--experiment", "missing"],
    )

    # Then: the empty report is explicit and no judge review starts.
    assert result.exit_code == 0, result.output
    assert "no traces" in result.output.lower()
    run_from_env.assert_not_called()


def test_judge_uses_dataset_id_to_disambiguate_experiment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a requested dataset resolves to its v4 identifier.
    provider = MagicMock(spec=LLMProvider)
    captured: dict[str, str | None] = {}
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(judge_cli, "build_judge_provider_from_env", lambda: provider)
    monkeypatch.setattr(
        judge_cli,
        "Langfuse",
        lambda **_kwargs: SimpleNamespace(
            get_dataset=lambda _name: SimpleNamespace(id="dataset-1")
        ),
    )

    def fetch_experiments(
        **kwargs: str | dict[str, str] | None,
    ) -> list[dict[str, JsonValue]]:
        dataset_id = kwargs["dataset_id"]
        assert isinstance(dataset_id, str)
        captured["dataset_id"] = dataset_id
        return [
            {
                "name": "baseline-v1",
                "datasetId": dataset_id,
                "startTime": "2026-08-20T00:00:00Z",
                "endTime": "2026-08-21T00:00:00Z",
            }
        ]

    async def run_from_env(
        _config: JudgePassConfig,
        _experiment: ExperimentWindow,
        _environment: JudgePassEnvironment,
    ) -> None:
        return None

    monkeypatch.setattr(eval_cli, "_fetch_experiments", fetch_experiments)
    monkeypatch.setattr(
        "bot.eval.judge_pass.run_judge_pass_from_env",
        run_from_env,
        raising=False,
    )

    # When: the judge pass includes --dataset.
    result = CliRunner().invoke(
        eval_cli.app,
        ["judge", "--experiment", "baseline-v1", "--dataset", "math-qa"],
    )

    # Then: experiment listing is scoped by the resolved dataset identifier.
    assert result.exit_code == 0, result.output
    assert captured == {"dataset_id": "dataset-1"}
