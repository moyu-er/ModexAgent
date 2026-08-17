from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bot.eval import cli as eval_cli
from click.testing import Result
from langfuse.api.commons.errors.not_found_error import NotFoundError
from typer.testing import CliRunner


def _mock_langfuse(*, dataset_id: str = "ds-123", raise_not_found: bool = False) -> Any:
    if raise_not_found:
        def raise_nf(name: str) -> None:
            raise NotFoundError({"message": "Dataset not found"})
        return SimpleNamespace(get_dataset=raise_nf)
    return SimpleNamespace(
        get_dataset=lambda name: SimpleNamespace(id=dataset_id, name=name),
    )


def _mock_fetch_experiments(experiments: list[dict[str, Any]]) -> None:
    def _fetch(*, host: str, headers: dict[str, str], dataset_id: str) -> list[dict[str, Any]]:
        return [exp for exp in experiments if exp.get("datasetId") == dataset_id]
    eval_cli._fetch_experiments = _fetch


def _mock_fetch_experiment_scores(scores: str) -> None:
    eval_cli._fetch_experiment_scores = lambda **_kwargs: scores


def _invoke_compare(
    monkeypatch: pytest.MonkeyPatch,
    dataset: str,
    *,
    langfuse: Any | None = None,
    experiments: list[dict[str, Any]] | None = None,
    scores: str = "(no scores)",
) -> Result:
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(
        eval_cli,
        "Langfuse",
        lambda **_kwargs: langfuse or _mock_langfuse(),
    )
    _mock_fetch_experiments(experiments or [])
    _mock_fetch_experiment_scores(scores)
    return CliRunner().invoke(eval_cli.app, ["compare", "--dataset", dataset])


def test_compare_reports_dataset_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    langfuse = _mock_langfuse(raise_not_found=True)

    result = _invoke_compare(monkeypatch, "no-such-dataset", langfuse=langfuse)

    assert result.exit_code == 0, result.output
    assert "not found" in result.output


def test_compare_shows_no_experiments(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke_compare(monkeypatch, "empty-dataset", experiments=[])

    assert result.exit_code == 0, result.output
    assert "No experiment runs found" in result.output
    assert "evals/runs/" in result.output


def test_compare_lists_experiments_with_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    experiments = [
        {
            "id": "exp-1",
            "name": "baseline-v1",
            "startTime": "2026-08-17T04:17:42.000Z",
            "endTime": "2026-08-17T04:17:43.500Z",
            "itemCount": 2,
            "datasetId": "ds-123",
        },
        {
            "id": "exp-2",
            "name": "prompt-v2",
            "startTime": "2026-08-17T05:00:00.000Z",
            "endTime": "2026-08-17T05:01:00.000Z",
            "itemCount": 3,
            "datasetId": "ds-123",
        },
    ]

    result = _invoke_compare(
        monkeypatch,
        "math-qa",
        experiments=experiments,
        scores="accuracy=100%, completion=100%",
    )

    assert result.exit_code == 0, result.output
    assert "baseline-v1" in result.output
    assert "prompt-v2" in result.output
    assert "accuracy=100%" in result.output
    assert "completion=100%" in result.output


def test_compare_filters_experiments_by_dataset_id(monkeypatch: pytest.MonkeyPatch) -> None:
    experiments = [
        {
            "id": "exp-1",
            "name": "belongs-to-us",
            "startTime": "2026-08-17T04:17:42.000Z",
            "endTime": "2026-08-17T04:17:43.500Z",
            "itemCount": 1,
            "datasetId": "ds-123",
        },
        {
            "id": "exp-2",
            "name": "belongs-to-other",
            "startTime": "2026-08-17T05:00:00.000Z",
            "endTime": "2026-08-17T05:01:00.000Z",
            "itemCount": 1,
            "datasetId": "ds-other",
        },
    ]

    result = _invoke_compare(
        monkeypatch,
        "math-qa",
        experiments=experiments,
        scores="accuracy=50%",
    )

    assert result.exit_code == 0, result.output
    assert "belongs-to-us" in result.output
    assert "belongs-to-other" not in result.output
