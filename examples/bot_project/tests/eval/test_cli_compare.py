from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from bot.eval import cli as eval_cli
from click.testing import Result
from langfuse.api.commons.errors.not_found_error import NotFoundError
from typer.testing import CliRunner

from modex_agent.trace.langfuse_query import ObservationData, ScoreReadData


def _mock_langfuse(*, dataset_id: str = "ds-123", raise_not_found: bool = False) -> Any:
    if raise_not_found:
        def raise_nf(name: str) -> None:
            raise NotFoundError({"message": "Dataset not found"})
        return SimpleNamespace(get_dataset=raise_nf)
    return SimpleNamespace(
        get_dataset=lambda name: SimpleNamespace(id=dataset_id, name=name),
    )


def _mock_fetch_experiments(
    monkeypatch: pytest.MonkeyPatch,
    experiments: list[dict[str, Any]],
) -> None:
    def _fetch(*, host: str, headers: dict[str, str], dataset_id: str) -> list[dict[str, Any]]:
        return [exp for exp in experiments if exp.get("datasetId") == dataset_id]
    monkeypatch.setattr(eval_cli, "_fetch_experiments", _fetch)


def _mock_fetch_experiment_scores(
    monkeypatch: pytest.MonkeyPatch,
    scores: str,
) -> None:
    monkeypatch.setattr(eval_cli, "_fetch_experiment_scores", lambda **_kwargs: scores)


def _mock_fetch_experiment_costs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fetch(
        _host: str,
        _credentials: tuple[str, str],
        experiments: list[dict[str, Any]],
    ) -> list[eval_cli.CostSummary]:
        return [
            eval_cli.CostSummary(total_usd=1.5, mean_usd=0.5, count=3)
            for _ in experiments
        ]

    monkeypatch.setattr(eval_cli, "_fetch_experiment_costs", _fetch)


def _mock_fetch_experiment_judge_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fetch(
        _host: str,
        _credentials: tuple[str, str],
        _experiments: list[dict[str, Any]],
    ) -> list[list[ScoreReadData] | None]:
        return [None] * len(_experiments)

    monkeypatch.setattr(
        eval_cli,
        "_fetch_experiment_judge_scores",
        _fetch,
        raising=False,
    )


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
    _mock_fetch_experiments(monkeypatch, experiments or [])
    _mock_fetch_experiment_scores(monkeypatch, scores)
    _mock_fetch_experiment_costs(monkeypatch)
    _mock_fetch_experiment_judge_scores(monkeypatch)
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
    assert "Cost" in result.output
    assert "sum=$1.500000 mean=$0.500000" in result.output


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


async def test_fetch_experiment_cost_paginates_past_100_scores() -> None:
    # Given
    first_page = [
        ScoreReadData(name="cost_usd", value=0.01, data_type="NUMERIC")
        for _ in range(100)
    ]
    second_page = [ScoreReadData(name="cost_usd", value=0.5, data_type="NUMERIC")]
    client = AsyncMock()
    client.get_scores.side_effect = [
        (first_page, "next-page"),
        (second_page, None),
    ]

    # When
    summary = await eval_cli._fetch_experiment_cost(
        client,
        start_time="2026-08-17T04:17:42.000Z",
        end_time="2026-08-17T04:17:43.500Z",
    )

    # Then
    assert summary.count == 101
    assert summary.total_usd == pytest.approx(1.5)
    assert summary.mean_usd == pytest.approx(1.5 / 101)
    assert client.get_scores.await_count == 2
    assert client.get_scores.await_args_list[0].kwargs == {
        "fields": "core,details,subject",
        "name": "cost_usd",
        "from_timestamp": "2026-08-17T04:17:42.000Z",
        "to_timestamp": "2026-08-17T04:17:45.500000Z",
        "limit": 100,
        "cursor": None,
    }
    assert client.get_scores.await_args_list[1].kwargs["cursor"] == "next-page"


async def test_fetch_experiment_cost_stops_at_max_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setattr(eval_cli, "_MAX_PAGES", 2)
    client = AsyncMock()
    client.get_scores.side_effect = [
        ([ScoreReadData(name="cost_usd", value=1.0, data_type="NUMERIC")], "page-2"),
        ([ScoreReadData(name="cost_usd", value=2.0, data_type="NUMERIC")], "page-3"),
    ]

    # When
    summary = await eval_cli._fetch_experiment_cost(
        client,
        start_time="2026-08-17T04:17:42.000Z",
        end_time="2026-08-17T04:17:43.500Z",
    )

    # Then
    assert summary.total_usd == 3.0
    assert summary.count == 2
    assert client.get_scores.await_count == 2


def test_fetch_experiment_scores_marks_uncalibrated_judge_and_requests_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured_params: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": "judge_task_completion",
                        "value": 0.5,
                        "comment": json.dumps(
                            {
                                "scorer": "judge",
                                "version": "judge.v1+abc12345",
                                "report_source": "llm_judge",
                                "run_ref": "evals/runs/judge/baseline-v1",
                                "calibrated": False,
                            }
                        ),
                    }
                ]
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        eval_cli.httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(handler)),
    )

    # When
    summary = eval_cli._fetch_experiment_scores(
        host="http://localhost:3000",
        headers={"Authorization": "Basic token"},
        start_time="2026-08-17T04:17:42.000Z",
        end_time="2026-08-17T04:17:43.500Z",
    )

    # Then
    assert summary == "judge_task_completion=50%*"
    assert captured_params["fields"] == "core,details,subject"


def test_fetch_experiment_scores_preserves_non_judge_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"name": "accuracy", "value": 0.5, "comment": None}]},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        eval_cli.httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(handler)),
    )

    # When
    summary = eval_cli._fetch_experiment_scores(
        host="http://localhost:3000",
        headers={"Authorization": "Basic token"},
        start_time="2026-08-17T04:17:42.000Z",
        end_time="2026-08-17T04:17:43.500Z",
    )

    # Then
    assert summary == "accuracy=50%"


def test_compare_renders_posthoc_uncalibrated_judge_score_from_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    experiment = {
        "id": "exp-1",
        "name": "posthoc-judge",
        "startTime": "2026-08-17T04:17:42.000Z",
        "endTime": "2026-08-17T04:18:42.000Z",
        "itemCount": 1,
        "datasetId": "ds-123",
    }
    root_observation = ObservationData(
        id="root-1",
        trace_id="trace-1",
        start_time=datetime(2026, 8, 17, 4, 17, 42, tzinfo=UTC),
        end_time=datetime(2026, 8, 17, 4, 18, 42, tzinfo=UTC),
        parent_observation_id=None,
        type="AGENT",
        name="invoke_agent",
        level="DEFAULT",
        input=None,
        output=None,
        usage_details=None,
        metadata=None,
        provided_model_name=None,
        session_id=None,
        latency=None,
        status_message=None,
    )
    judge_score = ScoreReadData(
        name="judge_rubric_overall",
        value=0.5,
        data_type="NUMERIC",
        comment=json.dumps(
            {
                "scorer": "judge",
                "version": "judge.v1+abc12345",
                "report_source": "llm_judge",
                "run_ref": "evals/runs/judge/posthoc-judge",
                "calibrated": False,
            }
        ),
    )
    client = AsyncMock()
    client.get_observations.return_value = ([root_observation], None)
    client.get_scores.return_value = ([judge_score], None)
    monkeypatch.setattr(
        eval_cli,
        "_load_langfuse_env",
        lambda: ("http://localhost:3000", "public", "secret"),
    )
    monkeypatch.setattr(
        eval_cli,
        "Langfuse",
        lambda **_kwargs: _mock_langfuse(),
    )
    monkeypatch.setattr(eval_cli, "LangfuseClient", lambda *_args: client)
    _mock_fetch_experiments(monkeypatch, [experiment])
    _mock_fetch_experiment_costs(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"name": "accuracy", "value": 0.5, "comment": None}]},
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        eval_cli.httpx,
        "Client",
        lambda **_kwargs: real_client(transport=httpx.MockTransport(handler)),
    )

    # When
    result = CliRunner().invoke(
        eval_cli.app,
        ["compare", "--dataset", "posthoc-judge"],
    )

    # Then
    assert result.exit_code == 0, result.output
    assert "accuracy=50%" in result.output
    assert "judge_rubric_overall=50%*" in result.output
    client.get_scores.assert_awaited_once_with(
        fields="core,details,subject",
        trace_id="trace-1",
        limit=100,
        cursor=None,
    )
