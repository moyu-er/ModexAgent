"""Live-observability tests for the tb21 batch runner.

Hermetic: no docker, no network, no LLM. `run_trial`, the Langfuse dataset
factory, the model env, and the verdict collector are patched; the batch
runner itself (task-start JSON, periodic refresher, interrupted markers)
runs for real against a temp evidence tree.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bot.eval.harbor import tb21_batch
from bot.eval.harbor.verdict_collector import TrialTraceMap

TrialFn = Callable[[Any, Any, Any], Awaitable[Any]]


def _make_dataset(tmp_path: Path, *names: str) -> Path:
    dataset = tmp_path / "dataset"
    for name in names:
        (dataset / name).mkdir(parents=True)
    return dataset


def _evidence_dir(repo_root: Path, run_id: str) -> Path:
    return repo_root / "examples" / "bot_project" / "evals" / "evidence" / "tb21" / run_id


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _trial_result() -> SimpleNamespace:
    return SimpleNamespace(result=SimpleNamespace(exit_code=0))


def _patch_batch_env(monkeypatch: pytest.MonkeyPatch, run_trial: TrialFn) -> None:
    async def fake_create_dataset(
        dataset_dir: Path, experiment_name: str
    ) -> tuple[str, dict[str, str]]:
        names = sorted(p.name for p in dataset_dir.iterdir() if p.is_dir())
        return "dataset-fake", {name: f"item-{name}" for name in names}

    monkeypatch.setattr(tb21_batch, "_create_tb_dataset", fake_create_dataset)
    monkeypatch.setattr(tb21_batch, "run_trial", run_trial)
    monkeypatch.setattr(tb21_batch, "SubprocessExecutionPlane", lambda: object())
    monkeypatch.setattr(
        "bot.eval.harbor.model_source.resolve_model_settings",
        lambda *args: SimpleNamespace(model="openai/fake-model"),
    )
    monkeypatch.setattr(
        "bot.eval.harbor.model_source.inject_model_env",
        lambda settings: None,
    )
    monkeypatch.setattr("bot.eval.harbor.host_cli.collect_job", AsyncMock(return_value=0))
    monkeypatch.setattr(
        "bot.eval.harbor.verdict_collector.read_trial_trace_map",
        lambda job_dir: TrialTraceMap(entries=()),
    )
    monkeypatch.setattr(
        "bot.eval.harbor.verdict_collector.read_official_results",
        lambda job_dir: (),
    )
    # pre-set so the direct os.environ assignment inside _run_batch is a no-op
    # that monkeypatch restores afterwards
    monkeypatch.setenv("MODEX_AGENT_MODE", "pool")


async def test_task_start_json_written_before_trial_completes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset = _make_dataset(tmp_path, "task-a", "task-b")
    evidence = _evidence_dir(tmp_path, "run-start")
    started_snapshots: dict[str, dict[str, Any]] = {}

    async def fake_run_trial(request: Any, plane: Any, minter: Any) -> Any:
        name = request.job_name
        started_snapshots[name] = _read_json(evidence / f"{name}.json")
        return _trial_result()

    _patch_batch_env(monkeypatch, fake_run_trial)

    await tb21_batch._run_batch("run-start", dataset, tmp_path, timeout_multiplier=1.0)

    assert set(started_snapshots) == {"task-a", "task-b"}
    for name, snapshot in started_snapshots.items():
        assert snapshot["task"] == name
        assert snapshot["status"] == "running"
        assert snapshot["elapsed_s"] is None
        assert snapshot["started_at"]
    # after completion the running marker is replaced by the final record
    final = _read_json(evidence / "task-a.json")
    assert final["status"] in {"completed", "verifier_error"}
    assert final["elapsed_s"] is not None


async def test_refresher_updates_elapsed_s_while_task_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset = _make_dataset(tmp_path, "task-a")
    evidence = _evidence_dir(tmp_path, "run-elapsed")
    snapshots: list[dict[str, Any]] = []

    async def fake_run_trial(request: Any, plane: Any, minter: Any) -> Any:
        snapshots.append(_read_json(evidence / "task-a.json"))
        await asyncio.sleep(0.3)
        snapshots.append(_read_json(evidence / "task-a.json"))
        snapshots.append(_read_json(evidence / "report.json"))
        return _trial_result()

    _patch_batch_env(monkeypatch, fake_run_trial)

    await tb21_batch._run_batch(
        "run-elapsed", dataset, tmp_path, timeout_multiplier=1.0, refresh_interval_s=0.05
    )

    start_snapshot, mid_snapshot, report_mid = snapshots
    assert start_snapshot["status"] == "running"
    assert start_snapshot["elapsed_s"] is None
    assert mid_snapshot["status"] == "running"
    assert mid_snapshot["elapsed_s"] is not None
    assert mid_snapshot["elapsed_s"] > 0
    # report.json exists mid-run, before any task completed
    assert report_mid["attempted"] == 0


async def test_refresher_reflects_worker_pool_shared_running_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset = _make_dataset(tmp_path, "task-a", "task-b")
    evidence = _evidence_dir(tmp_path, "run-pool")
    mid_snapshots: dict[str, dict[str, Any]] = {}
    dashboard_seen: list[str] = []

    async def fake_run_trial(request: Any, plane: Any, minter: Any) -> Any:
        name = request.job_name
        await asyncio.sleep(0.25)
        mid_snapshots[name] = _read_json(evidence / f"{name}.json")
        dashboard_seen.append((evidence / "dashboard.html").read_text(encoding="utf-8"))
        return _trial_result()

    _patch_batch_env(monkeypatch, fake_run_trial)

    await tb21_batch._run_batch(
        "run-pool",
        dataset,
        tmp_path,
        timeout_multiplier=1.0,
        concurrency=2,
        refresh_interval_s=0.05,
    )

    assert set(mid_snapshots) == {"task-a", "task-b"}
    for name, snapshot in mid_snapshots.items():
        assert snapshot["task"] == name
        assert snapshot["status"] == "running"
        assert snapshot["elapsed_s"] is not None
        assert snapshot["elapsed_s"] > 0
    # dashboard refreshes ride the worker pool's shared running set
    html = dashboard_seen[0]
    assert "task-a" in html
    assert "task-b" in html


async def test_interrupted_marker_written_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset = _make_dataset(tmp_path, "task-a", "task-b")
    evidence = _evidence_dir(tmp_path, "run-interrupted")

    async def fake_run_trial(request: Any, plane: Any, minter: Any) -> Any:
        raise KeyboardInterrupt

    _patch_batch_env(monkeypatch, fake_run_trial)

    with pytest.raises(KeyboardInterrupt):
        await tb21_batch._run_batch("run-interrupted", dataset, tmp_path, timeout_multiplier=1.0)

    interrupted = _read_json(evidence / "task-a.json")
    assert interrupted["status"] == "interrupted"
    assert interrupted["elapsed_s"] is not None
    # task-b never started; the evidence dir must not claim otherwise
    assert not (evidence / "task-b.json").exists()
    # the finally block still flushed report + dashboard before exit
    assert _read_json(evidence / "report.json")["attempted"] == 0
    assert (evidence / "dashboard.html").exists()


def test_atomic_write_replaces_existing_file_and_leaves_no_temp(tmp_path: Path) -> None:
    path = tmp_path / "task-a.json"
    path.write_text('{"status": "old"}', encoding="utf-8")

    tb21_batch._atomic_write(path, '{"status": "running"}')

    assert path.read_text(encoding="utf-8") == '{"status": "running"}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_creates_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "report.json"

    tb21_batch._atomic_write(path, "{}")

    assert path.read_text(encoding="utf-8") == "{}"
    assert list(tmp_path.glob("*.tmp")) == []


def _chat_span(input_tokens: int, output_tokens: int) -> str:
    return json.dumps(
        {
            "name": "chat",
            "attributes": {
                "gen_ai.usage.input_tokens": input_tokens,
                "gen_ai.usage.output_tokens": output_tokens,
            },
        }
    )


def _agent_dir(job_dir: Path, trial: str = "task-x__abc123") -> Path:
    agent = job_dir / trial / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    return agent


def _spans_dir(agent_dir: Path, session: str) -> Path:
    spans = agent_dir / "pool-data" / "runtime_state" / "coder" / "trace" / session
    spans.mkdir(parents=True, exist_ok=True)
    return spans


def test_job_metrics_reads_result_and_usage_artifacts(tmp_path: Path) -> None:
    agent = _agent_dir(tmp_path / "job-full")
    (agent / "result.json").write_text(
        json.dumps({"trace_id": "t-1", "stop_reason": "completed", "spent_usd": 0.42}),
        encoding="utf-8",
    )
    (agent / "usage.json").write_text(
        json.dumps({"model": "m", "input_tokens": 5299, "output_tokens": 50000}),
        encoding="utf-8",
    )

    assert tb21_batch._job_metrics(tmp_path / "job-full") == {
        "stop_reason": "completed",
        "spent_usd": 0.42,
        "input_tokens": 5299,
        "output_tokens": 50000,
    }


def test_job_metrics_spans_fallback_for_timeout_killed_trial(tmp_path: Path) -> None:
    agent = _agent_dir(tmp_path / "job-killed")
    session_a = _spans_dir(agent, "sess-a")
    session_b = _spans_dir(agent, "sess-b")
    # install-result.json must not be mistaken for the agent result artifact
    (agent / "install-result.json").write_text("{}", encoding="utf-8")
    (session_a / "spans.jsonl").write_text(
        "\n".join(
            (
                json.dumps({"name": "agent.start", "attributes": {}}),
                _chat_span(100, 200),
                "{not json",
                _chat_span(11, 22),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (session_b / "spans.jsonl").write_text(_chat_span(1000, 2000) + "\n", encoding="utf-8")

    assert tb21_batch._job_metrics(tmp_path / "job-killed") == {
        "stop_reason": "timeout_kill",
        "spent_usd": None,
        "input_tokens": 1111,
        "output_tokens": 2222,
    }


def test_job_metrics_empty_job_dir_is_all_none(tmp_path: Path) -> None:
    job = tmp_path / "job-empty"
    job.mkdir()

    assert tb21_batch._job_metrics(job) == {
        "stop_reason": None,
        "spent_usd": None,
        "input_tokens": None,
        "output_tokens": None,
    }


def test_job_metrics_reads_first_trial_only(tmp_path: Path) -> None:
    job = tmp_path / "job-retry"
    first_agent = _agent_dir(job, trial="task-x__aaa")
    second_agent = _agent_dir(job, trial="task-x__bbb")
    spans = _spans_dir(first_agent, "sess")
    (spans / "spans.jsonl").write_text(_chat_span(10, 20) + "\n", encoding="utf-8")
    (second_agent / "result.json").write_text(
        json.dumps({"stop_reason": "completed", "spent_usd": 1.0}), encoding="utf-8"
    )
    (second_agent / "usage.json").write_text(
        json.dumps({"input_tokens": 999, "output_tokens": 999}), encoding="utf-8"
    )

    metrics = tb21_batch._job_metrics(job)

    assert metrics["stop_reason"] == "timeout_kill"
    assert metrics["spent_usd"] is None
    assert metrics["input_tokens"] == 10
    assert metrics["output_tokens"] == 20


def test_write_report_aggregates_cost_tokens_and_stop_reasons(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    checkpoint = tmp_path / "checkpoint.jsonl"
    rows = [
        {
            "task": "a",
            "reward": 1.0,
            "stop_reason": "completed",
            "spent_usd": 0.5,
            "input_tokens": 100,
            "output_tokens": 200,
        },
        {
            "task": "b",
            "reward": 0.0,
            "stop_reason": "timeout_kill",
            "spent_usd": 1.25,
            "input_tokens": 50,
            "output_tokens": 60,
        },
        {"task": "c", "reward": None},
    ]
    checkpoint.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )

    path = tb21_batch._write_report(checkpoint, evidence, total=3)

    report = _read_json(path)
    assert report["total_spent_usd"] == 1.75
    assert report["total_input_tokens"] == 150
    assert report["total_output_tokens"] == 260
    assert report["stop_reason_histogram"] == {"completed": 1, "timeout_kill": 1}


async def test_run_batch_records_job_metrics_for_killed_trial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dataset = _make_dataset(tmp_path, "task-k")
    evidence = _evidence_dir(tmp_path, "run-metrics")

    async def fake_run_trial(request: Any, plane: Any, minter: Any) -> Any:
        spans = _spans_dir(
            _agent_dir(request.jobs_dir / request.job_name, trial=f"{request.job_name}__abc123"),
            "sess",
        )
        (spans / "spans.jsonl").write_text(_chat_span(100, 200) + "\n", encoding="utf-8")
        return _trial_result()

    _patch_batch_env(monkeypatch, fake_run_trial)

    await tb21_batch._run_batch("run-metrics", dataset, tmp_path, timeout_multiplier=1.0)

    record = _read_json(evidence / "task-k.json")
    assert record["stop_reason"] == "timeout_kill"
    assert record["spent_usd"] is None
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 200
    report = _read_json(evidence / "report.json")
    assert report["stop_reason_histogram"] == {"timeout_kill": 1}
    assert report["total_spent_usd"] is None
    assert report["total_input_tokens"] == 100
    assert report["total_output_tokens"] == 200
