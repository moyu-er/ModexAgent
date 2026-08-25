from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from bot.eval.harbor import smoke_gate, smoke_runtime
from bot.eval.harbor.host_runtime import HostCommand, HostCommandResult
from bot.eval.harbor.smoke_evidence import (
    REQUIRED_LANGFUSE_CONTAINERS,
    B6PoolSmokeEvidence,
    B6SmokeEvidence,
    SmokeCommandResult,
    SmokePreflight,
    SmokeRequest,
)
from bot.eval.harbor.smoke_gate import run_gate, run_preflight
from bot.eval.harbor.smoke_runtime import (
    SmokeDataset,
    SmokeDatasetItem,
    SmokeMode,
    SmokeRunRequest,
    SmokeRunResult,
    run_smoke,
)
from typer.testing import CliRunner

_LLM_ENV = ("LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL")
_MODEL_ENV = (
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "MODEX_TEMPERATURE",
    "MODEX_REASONING_EFFORT",
    "MODEX_MAX_CONTEXT_TOKENS",
)


def _set_dummy_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saturate every model-resolution env slot so the gate never reads model.yml."""
    for name, value in (
        ("LLM_MODEL", "openai/dummy-model"),
        ("LLM_API_KEY", "dummy-key"),
        ("LLM_BASE_URL", "http://dummy.example/v1"),
        ("MODEX_TEMPERATURE", "0.7"),
        ("MODEX_REASONING_EFFORT", "none"),
        ("MODEX_MAX_CONTEXT_TOKENS", "200000"),
    ):
        monkeypatch.setenv(name, value)


def _write_gate_model_yml(path: Path) -> Path:
    path.write_text(
        "default_provider: provider\n"
        "default_model: yml-model\n"
        "providers:\n"
        "  - key: provider\n"
        "    name: provider\n"
        "    base_url: http://yml.example/v1\n"
        "    api_key: yml-key\n"
        "    models:\n"
        "      - name: yml-model\n"
        "        model: yml-model\n"
        "        temperature: 0.3\n"
        "        reasoning_effort: low\n",
        encoding="utf-8",
    )
    return path


def _docker_ok_run(command: tuple[str, ...]) -> SmokeCommandResult:
    if command == ("docker", "info"):
        return SmokeCommandResult(exit_code=0)
    return SmokeCommandResult(
        exit_code=0,
        stdout="\n".join(sorted(REQUIRED_LANGFUSE_CONTAINERS)),
    )


def _pool_request(tmp_path: Path, evidence_path: Path) -> SmokeRequest:
    return SmokeRequest(
        task_paths=(tmp_path / "task-a", tmp_path / "task-b"),
        run_id="pool-1",
        model="openai/step-3.7-flash",
        timeout_multiplier=6.0,
        jobs_dir=tmp_path / "jobs",
        mode=SmokeMode.POOL,
        evidence_path=evidence_path,
    )


def _pool_run_result() -> SmokeRunResult:
    return SmokeRunResult(
        experiment_name="terminalbench.pool-1",
        job_dirs=("jobs/pool-1-1", "jobs/pool-1-2"),
        trace_ids=("trace-1", "trace-2"),
        verdicts=(1.0, 1.0),
        score_count=2,
        install_seconds=(412.5, 388.25),
        child_sessions=("conv.explore",),
        delegation_counts=(1, 2),
    )


def test_preflight_pool_lists_missing_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)

    preflight = run_preflight(_docker_ok_run, mode=SmokeMode.POOL)

    assert preflight.docker_daemon is True
    assert preflight.langfuse_stack is True
    for name in _LLM_ENV:
        assert f"env:{name}" in preflight.missing


def test_preflight_pool_passes_when_llm_env_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _LLM_ENV:
        monkeypatch.setenv(name, "value")
    monkeypatch.delenv("MODEX_BUDGET_USD", raising=False)

    preflight = run_preflight(_docker_ok_run, mode=SmokeMode.POOL)

    assert preflight.missing == ()


def test_preflight_pool_flags_malformed_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _LLM_ENV:
        monkeypatch.setenv(name, "value")
    monkeypatch.setenv("MODEX_BUDGET_USD", "not-money")

    preflight = run_preflight(_docker_ok_run, mode=SmokeMode.POOL)

    assert "env:MODEX_BUDGET_USD" in preflight.missing


def test_preflight_bare_ignores_host_env_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _LLM_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODEX_BUDGET_USD", "not-money")

    preflight = run_preflight(_docker_ok_run)

    assert preflight.missing == ()
    assert preflight.docker_daemon is True
    assert preflight.langfuse_stack is True


@pytest.mark.asyncio
async def test_run_gate_pool_writes_pool_evidence_with_default_pool_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in ("MODEX_POOL_NAME", "MODEX_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    _set_dummy_model_env(monkeypatch)
    monkeypatch.setattr(
        smoke_gate,
        "run_preflight",
        Mock(return_value=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=())),
    )
    run_smoke_mock = AsyncMock(return_value=_pool_run_result())
    monkeypatch.setattr(smoke_gate, "run_smoke", run_smoke_mock)
    evidence_path = tmp_path / "b6_pool_smoke.json"

    evidence = await run_gate(_pool_request(tmp_path, evidence_path))

    assert isinstance(evidence, B6PoolSmokeEvidence)
    assert evidence.passed is True
    assert evidence.gate == "b6_pool_smoke"
    assert evidence.mode == "pool"
    assert evidence.model == "openai/step-3.7-flash"
    assert evidence.model_source == "cli"
    assert evidence.pool_name == "coder"
    assert evidence.budget_usd == 25.0
    assert evidence.install_seconds == (412.5, 388.25)
    assert evidence.child_sessions == ("conv.explore",)
    assert evidence.delegation_counts == (1, 2)
    sent = run_smoke_mock.call_args.args[0]
    assert sent.mode is SmokeMode.POOL
    written = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert written["mode"] == "pool"
    assert written["gate"] == "b6_pool_smoke"
    assert written["model_source"] == "cli"
    assert written["pool_name"] == "coder"
    assert written["budget_usd"] == 25.0
    assert written["install_seconds"] == [412.5, 388.25]
    assert written["delegation_counts"] == [1, 2]


@pytest.mark.asyncio
async def test_run_gate_pool_records_host_pool_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODEX_POOL_NAME", "team")
    monkeypatch.setenv("MODEX_BUDGET_USD", "1.5")
    _set_dummy_model_env(monkeypatch)
    monkeypatch.setattr(
        smoke_gate,
        "run_preflight",
        Mock(return_value=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=())),
    )
    monkeypatch.setattr(smoke_gate, "run_smoke", AsyncMock(return_value=_pool_run_result()))
    evidence_path = tmp_path / "b6_pool_smoke.json"

    evidence = await run_gate(_pool_request(tmp_path, evidence_path))

    assert isinstance(evidence, B6PoolSmokeEvidence)
    assert evidence.pool_name == "team"
    assert evidence.budget_usd == 1.5


@pytest.mark.asyncio
async def test_run_gate_pool_records_preflight_failure_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_dummy_model_env(monkeypatch)
    monkeypatch.setattr(
        smoke_gate,
        "run_preflight",
        Mock(
            return_value=SmokePreflight(
                docker_daemon=True,
                langfuse_stack=True,
                missing=("env:LLM_API_KEY",),
            )
        ),
    )
    run_smoke_mock = AsyncMock(return_value=_pool_run_result())
    monkeypatch.setattr(smoke_gate, "run_smoke", run_smoke_mock)
    evidence_path = tmp_path / "b6_pool_smoke.json"

    evidence = await run_gate(_pool_request(tmp_path, evidence_path))

    assert evidence.passed is False
    assert "env:LLM_API_KEY" in (evidence.error or "")
    run_smoke_mock.assert_not_awaited()
    written = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert written["passed"] is False


@pytest.mark.asyncio
async def test_run_gate_resolves_model_from_yml_when_cli_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    yml = _write_gate_model_yml(tmp_path / "model.yml")
    monkeypatch.setattr(
        smoke_gate,
        "run_preflight",
        Mock(return_value=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=())),
    )
    run_smoke_mock = AsyncMock(return_value=_pool_run_result())
    monkeypatch.setattr(smoke_gate, "run_smoke", run_smoke_mock)
    request = SmokeRequest(
        task_paths=(tmp_path / "task-a", tmp_path / "task-b"),
        run_id="pool-yml",
        model=None,
        timeout_multiplier=6.0,
        jobs_dir=tmp_path / "jobs",
        mode=SmokeMode.POOL,
        evidence_path=tmp_path / "b6_pool_smoke.json",
        model_yml=yml,
    )

    try:
        evidence = await run_gate(request)
        injected = {name: os.environ.get(name) for name in _MODEL_ENV}
    finally:
        for name in _MODEL_ENV:
            os.environ.pop(name, None)

    assert isinstance(evidence, B6PoolSmokeEvidence)
    assert evidence.passed is True
    assert evidence.model == "openai/yml-model"
    assert evidence.model_source == "model-default"
    assert evidence.temperature == 0.3
    assert evidence.reasoning_effort == "low"
    sent = run_smoke_mock.call_args.args[0]
    assert sent.model == "openai/yml-model"
    assert injected == {
        "LLM_MODEL": "openai/yml-model",
        "LLM_API_KEY": "yml-key",
        "LLM_BASE_URL": "http://yml.example/v1",
        "MODEX_TEMPERATURE": "0.3",
        "MODEX_REASONING_EFFORT": "low",
        "MODEX_MAX_CONTEXT_TOKENS": "200000",
    }
    written = json.loads(
        (tmp_path / "b6_pool_smoke.json").read_text(encoding="utf-8")
    )
    assert written["model_source"] == "model-default"
    assert written["model"] == "openai/yml-model"
    assert written["temperature"] == 0.3
    assert written["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_run_gate_env_model_wins_over_yml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_MODEL", "openai/env-model")
    yml = _write_gate_model_yml(tmp_path / "model.yml")
    monkeypatch.setattr(
        smoke_gate,
        "run_preflight",
        Mock(return_value=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=())),
    )
    run_smoke_mock = AsyncMock(return_value=_pool_run_result())
    monkeypatch.setattr(smoke_gate, "run_smoke", run_smoke_mock)
    request = SmokeRequest(
        task_paths=(tmp_path / "task-a", tmp_path / "task-b"),
        run_id="pool-env",
        model=None,
        timeout_multiplier=6.0,
        jobs_dir=tmp_path / "jobs",
        mode=SmokeMode.POOL,
        evidence_path=tmp_path / "b6_pool_smoke.json",
        model_yml=yml,
    )

    try:
        evidence = await run_gate(request)
        model_in_env = os.environ.get("LLM_MODEL")
    finally:
        for name in _MODEL_ENV:
            os.environ.pop(name, None)

    assert isinstance(evidence, B6PoolSmokeEvidence)
    assert evidence.model == "openai/env-model"
    assert evidence.model_source == "env"
    assert model_in_env == "openai/env-model"


@pytest.mark.asyncio
async def test_run_gate_reports_model_source_failure_in_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in _MODEL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        smoke_gate,
        "run_preflight",
        Mock(return_value=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=())),
    )
    run_smoke_mock = AsyncMock(return_value=_pool_run_result())
    monkeypatch.setattr(smoke_gate, "run_smoke", run_smoke_mock)
    request = SmokeRequest(
        task_paths=(tmp_path / "task-a", tmp_path / "task-b"),
        run_id="pool-nosource",
        model=None,
        timeout_multiplier=6.0,
        jobs_dir=tmp_path / "jobs",
        mode=SmokeMode.POOL,
        evidence_path=tmp_path / "b6_pool_smoke.json",
        model_yml=tmp_path / "absent.yml",
    )

    evidence = await run_gate(request)

    assert isinstance(evidence, B6PoolSmokeEvidence)
    assert evidence.passed is False
    assert any("model source" in item for item in evidence.preflight.missing)
    assert "model source" in (evidence.error or "")
    assert evidence.model is None
    assert evidence.model_source is None
    run_smoke_mock.assert_not_awaited()


class _RecordingPlane:
    def __init__(self) -> None:
        self.commands: list[HostCommand] = []

    async def execute_host(self, command: HostCommand) -> HostCommandResult:
        self.commands.append(command)
        return HostCommandResult(exit_code=0)


def _write_pool_job(
    job_dir: Path,
    trial: str,
    *,
    install_seconds: float,
    child_sessions: tuple[str, ...],
    delegation_count: int,
) -> None:
    agent = job_dir / trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trace-ids.jsonl").write_text(
        json.dumps({"trace_id": f"trace-{trial}"}) + "\n", encoding="utf-8"
    )
    (agent / "install-result.json").write_text(
        json.dumps(
            {
                "task_result": "READY",
                "include_in_aggregate": True,
                "duration_seconds": install_seconds,
            }
        ),
        encoding="utf-8",
    )
    (agent / "usage.json").write_text(
        json.dumps(
            {
                "model": "openai/step-3.7-flash",
                "spent_usd": 0.01,
                "delegation": {
                    "main_session_id": "harbor.orchestrator",
                    "subagent_sessions": [],
                    "total_sessions": 2,
                    "delegation_count": delegation_count,
                },
            }
        ),
        encoding="utf-8",
    )
    (agent / "result.json").write_text(
        json.dumps(
            {
                "trace_id": f"trace-{trial}",
                "memory_namespace": "terminalbench.pool",
                "pool_name": "coder",
                "child_sessions": list(child_sessions),
            }
        ),
        encoding="utf-8",
    )
    (job_dir / trial / "result.json").write_text(
        json.dumps(
            {"trial_name": trial, "verifier_result": {"rewards": {"reward": 1.0}}}
        ),
        encoding="utf-8",
    )


def _write_bare_job(job_dir: Path, trial: str) -> None:
    agent = job_dir / trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trace-ids.jsonl").write_text(
        json.dumps({"trace_id": f"trace-{trial}"}) + "\n", encoding="utf-8"
    )
    (job_dir / trial / "result.json").write_text(
        json.dumps(
            {"trial_name": trial, "verifier_result": {"rewards": {"reward": 1.0}}}
        ),
        encoding="utf-8",
    )


def _patch_smoke_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _RecordingPlane:
    dataset = SmokeDataset(
        dataset_id="dataset-pool",
        items=(
            SmokeDatasetItem(task_path=tmp_path / "task-a", item_id="item-1"),
            SmokeDatasetItem(task_path=tmp_path / "task-b", item_id="item-2"),
        ),
    )
    monkeypatch.setattr(smoke_runtime, "_create_dataset", lambda paths: dataset)
    monkeypatch.setattr(smoke_runtime, "mint_stable_experiment_id", lambda request: "stable-exp-1")
    plane = _RecordingPlane()
    monkeypatch.setattr(smoke_runtime, "SubprocessExecutionPlane", lambda: plane)
    monkeypatch.setattr(smoke_runtime, "collect_job", AsyncMock())
    monkeypatch.setattr(
        smoke_runtime, "_read_back_verdict_count", AsyncMock(return_value=2)
    )
    return plane


@pytest.mark.asyncio
async def test_run_smoke_pool_dispatches_pool_env_and_reads_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LLM_MODEL", "openai/step-3.7-flash")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("MODEX_POOL_NAME", "coder")
    monkeypatch.setenv("MODEX_BUDGET_USD", "1.5")
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    jobs_dir = tmp_path / "jobs"
    _write_pool_job(
        jobs_dir / "pool-run-1",
        "trial-a",
        install_seconds=412.5,
        child_sessions=("c1.explore",),
        delegation_count=1,
    )
    _write_pool_job(
        jobs_dir / "pool-run-2",
        "trial-b",
        install_seconds=388.25,
        child_sessions=("c2.explore", "c2.general"),
        delegation_count=2,
    )
    plane = _patch_smoke_harness(monkeypatch, tmp_path)

    result = await run_smoke(
        SmokeRunRequest(
            task_paths=(tmp_path / "task-a", tmp_path / "task-b"),
            run_id="pool-run",
            model="openai/step-3.7-flash",
            timeout_multiplier=6.0,
            jobs_dir=jobs_dir,
            mode=SmokeMode.POOL,
        )
    )

    assert os.environ.get("MODEX_AGENT_MODE") == "pool"
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    assert result.install_seconds == (412.5, 388.25)
    assert result.child_sessions == ("c1.explore", "c2.explore", "c2.general")
    assert result.delegation_counts == (1, 2)
    assert result.score_count == 2
    assert result.verdicts == (1.0, 1.0)
    assert len(plane.commands) == 2
    for command in plane.commands:
        assert command.environment["MODEX_AGENT_MODE"] == "pool"
        assert command.environment["MODEX_POOL_NAME"] == "coder"
        assert command.environment["MODEX_BUDGET_USD"] == "1.5"
        assert command.environment["LLM_API_KEY"] == "key"
        assert "MODEX_AGENT_MODE=pool" in command.argv


@pytest.mark.asyncio
async def test_run_smoke_bare_does_not_inject_pool_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    jobs_dir = tmp_path / "jobs"
    _write_bare_job(jobs_dir / "bare-run-1", "trial-a")
    _write_bare_job(jobs_dir / "bare-run-2", "trial-b")
    plane = _patch_smoke_harness(monkeypatch, tmp_path)

    result = await run_smoke(
        SmokeRunRequest(
            task_paths=(tmp_path / "task-a", tmp_path / "task-b"),
            run_id="bare-run",
            model="openai/step-3.7-flash",
            timeout_multiplier=6.0,
            jobs_dir=jobs_dir,
        )
    )

    assert "MODEX_AGENT_MODE" not in os.environ
    assert all("MODEX_AGENT_MODE" not in c.environment for c in plane.commands)
    assert all("MODEX_AGENT_MODE=pool" not in c.argv for c in plane.commands)
    assert result.install_seconds == ()
    assert result.child_sessions == ()
    assert result.delegation_counts == ()
    assert result.score_count == 2


def _gate_mock(evidence: B6SmokeEvidence | B6PoolSmokeEvidence) -> AsyncMock:
    return AsyncMock(return_value=evidence)


def test_cli_mode_pool_selects_pool_evidence_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = B6PoolSmokeEvidence(
        passed=True,
        checked_at=datetime.now(UTC),
        preflight=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=()),
        run_id="pool-1",
        experiment_name="terminalbench.pool-1",
        task_paths=("a", "b"),
        pool_name="coder",
        budget_usd=5.0,
    )
    gate = _gate_mock(evidence)
    monkeypatch.setattr(smoke_gate, "run_gate", gate)

    result = CliRunner().invoke(
        smoke_gate.app,
        [
            "--task-path", "a",
            "--task-path", "b",
            "--run-id", "pool-1",
            "--model", "m",
            "--mode", "pool",
        ],
    )

    assert result.exit_code == 0
    request = gate.call_args.args[0]
    assert request.mode is SmokeMode.POOL
    assert request.evidence_path.name == "b6_pool_smoke.json"


def test_cli_default_mode_is_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = B6SmokeEvidence(
        passed=True,
        checked_at=datetime.now(UTC),
        preflight=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=()),
        run_id="bare-1",
        experiment_name="terminalbench.bare-1",
        task_paths=("a", "b"),
    )
    gate = _gate_mock(evidence)
    monkeypatch.setattr(smoke_gate, "run_gate", gate)

    result = CliRunner().invoke(
        smoke_gate.app,
        ["--task-path", "a", "--task-path", "b", "--run-id", "bare-1", "--model", "m"],
    )

    assert result.exit_code == 0
    request = gate.call_args.args[0]
    assert request.mode is SmokeMode.BARE
    assert request.evidence_path.name == "b6_smoke.json"


def test_cli_model_is_optional_and_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = B6SmokeEvidence(
        passed=True,
        checked_at=datetime.now(UTC),
        preflight=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=()),
        run_id="bare-yml",
        experiment_name="terminalbench.bare-yml",
        task_paths=("a", "b"),
    )
    gate = _gate_mock(evidence)
    monkeypatch.setattr(smoke_gate, "run_gate", gate)

    result = CliRunner().invoke(
        smoke_gate.app,
        ["--task-path", "a", "--task-path", "b", "--run-id", "bare-yml"],
    )

    assert result.exit_code == 0
    request = gate.call_args.args[0]
    assert request.model is None
    assert request.model_yml is None


def test_cli_model_yml_override_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = B6PoolSmokeEvidence(
        passed=True,
        checked_at=datetime.now(UTC),
        preflight=SmokePreflight(docker_daemon=True, langfuse_stack=True, missing=()),
        run_id="pool-yml",
        experiment_name="terminalbench.pool-yml",
        task_paths=("a", "b"),
        pool_name="coder",
        budget_usd=5.0,
    )
    gate = _gate_mock(evidence)
    monkeypatch.setattr(smoke_gate, "run_gate", gate)

    result = CliRunner().invoke(
        smoke_gate.app,
        [
            "--task-path", "a",
            "--task-path", "b",
            "--run-id", "pool-yml",
            "--mode", "pool",
            "--model-yml", "custom/model.yml",
        ],
    )

    assert result.exit_code == 0
    request = gate.call_args.args[0]
    assert request.model is None
    assert request.model_yml == Path("custom/model.yml")

