from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from bot.eval import judge_cli
from bot.eval.harbor import host_cli, smoke_gate
from bot.eval.harbor.agent import (
    APT_PROBE_COMMAND,
    POOL_MODE_ENV_VARS,
    PYTHON_PROBE_COMMAND,
    UV_PROBE_COMMAND,
    UV_PYTHON_INSTALL_MIRROR_ENV,
    CommandExecution,
    CommandResult,
    HarborTaskResult,
    InstallProbeResult,
)
from bot.eval.harbor.host_cli import (
    HostCommand,
    HostCommandResult,
    HostExecutionPlane,
    HostInstallRequest,
    RunTrialRequest,
    SubprocessExecutionPlane,
    app,
    install_host,
    run_trial,
)
from bot.eval.harbor.host_runtime import _agent_timeout_multiplier
from bot.eval.harbor.smoke_gate import (
    REQUIRED_LANGFUSE_CONTAINERS,
    SmokeCommandResult,
    run_preflight,
)
from bot.eval.harbor.verdict_collector import (
    TerminalBenchResult,
    TrialTrace,
    TrialTraceMap,
    VerdictProvenance,
    collect_verdict_scores,
    read_official_results,
    read_trial_trace_map,
)
from typer.testing import CliRunner


class RecordingPlane(HostExecutionPlane):
    def __init__(self, probe: InstallProbeResult) -> None:
        self.probe = probe
        self.uploads: list[tuple[str, Path, str]] = []
        self.executions: list[CommandExecution] = []
        self.host_commands: list[HostCommand] = []

    async def probe_install(self, container: str) -> InstallProbeResult:
        return self.probe

    async def upload_file(self, container: str, source: Path, target: str) -> None:
        self.uploads.append((container, source, target))

    async def execute_install(
        self,
        container: str,
        execution: CommandExecution,
    ) -> CommandResult:
        self.executions.append(execution)
        return CommandResult(exit_code=0)

    async def execute_host(self, command: HostCommand) -> HostCommandResult:
        self.host_commands.append(command)
        return HostCommandResult(exit_code=0)


@pytest.mark.asyncio
async def test_install_when_ubuntu_has_apt_runs_normal_install_flow(tmp_path: Path) -> None:
    plane = RecordingPlane(InstallProbeResult(python_available=False, apt_available=True))
    archive = tmp_path / "modex-src.tar.gz"

    result = await install_host(
        HostInstallRequest(
            repo_root=Path(__file__).resolve().parents[5],
            archive_path=archive,
            container="trial-ubuntu",
        ),
        plane,
    )

    assert result.task_result is HarborTaskResult.READY
    assert plane.uploads == [("trial-ubuntu", archive, "/tmp/modex-src.tar.gz")]
    assert plane.executions[0].command.argv == ("apt-get", "update")
    assert len(plane.executions) == 7


@pytest.mark.asyncio
async def test_install_when_no_python_or_apt_is_the_only_no_test_tier(tmp_path: Path) -> None:
    plane = RecordingPlane(InstallProbeResult(python_available=False, apt_available=False))

    result = await install_host(
        HostInstallRequest(
            repo_root=Path(__file__).resolve().parents[5],
            archive_path=tmp_path / "unused.tar.gz",
            container="trial-minimal",
        ),
        plane,
    )

    assert result.task_result is HarborTaskResult.NO_TEST
    assert result.include_in_aggregate is False
    assert plane.uploads == []
    assert plane.executions == []


@pytest.mark.asyncio
async def test_run_mints_stable_id_and_injects_container_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "inherited-python-path")
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))
    request = RunTrialRequest(
        task_path=tmp_path / "task-one",
        jobs_dir=tmp_path / "jobs",
        job_name="b6-one",
        experiment_name="terminalbench.b6-smoke",
        dataset_id="dataset-1",
        item_id="item-1",
        model="openai/step-3.7-flash",
        memory_namespace="terminalbench.b6-smoke",
        timeout_multiplier=6.0,
        compose_overlay=Path("bot/eval/harbor/docker-compose.uv.yml"),
    )

    result = await run_trial(request, plane, lambda _request: "stable-exp-1")

    assert result.experiment_id == "stable-exp-1"
    command = plane.host_commands[0]
    assert command.argv[:5] == ("harbor", "run", "--path", str(request.task_path), "--agent")
    assert "--timeout-multiplier" in command.argv
    assert "6.0" in command.argv
    assert command.environment["MODEX_EXPERIMENT_ID"] == "stable-exp-1"
    assert command.environment["MODEX_EXPERIMENT_ITEM_ID"] == "item-1"
    assert command.environment["MODEX_MEMORY_NS"] == request.memory_namespace
    assert command.environment["MODEX_TASK_NAME"] == "task-one"
    repo_root = Path(__file__).resolve().parents[5]
    assert command.environment["PYTHONPATH"].split(os.pathsep) == [
        str(repo_root / "src"),
        str(repo_root / "examples" / "bot_project"),
        "inherited-python-path",
    ]
    assert command.environment["PYTHONIOENCODING"] == "utf-8"
    assert command.environment["PYTHONUTF8"] == "1"


_POOL_MODE_HOST_ENV = {
    "LANGFUSE_HOST": "http://langfuse.invalid",
    "MODEX_AGENT_MODE": "pool",
    "MODEX_POOL_NAME": "coder",
    "MODEX_BUDGET_USD": "1.5",
    "MODEX_APPROVAL": "off",
    "MODEX_BOT_PROJECT_DIR": "/opt/modex/examples/bot_project",
    "MODEX_MAX_CONTEXT_TOKENS": "500000",
    "OTEL_FORMAT": "otel_http",
}


def _pool_trial_request(tmp_path: Path) -> RunTrialRequest:
    return RunTrialRequest(
        task_path=tmp_path / "task-pool",
        jobs_dir=tmp_path / "jobs",
        job_name="pool-one",
        experiment_name="terminalbench.pool",
        dataset_id="dataset-pool",
        item_id="item-pool",
        model="openai/step-3.7-flash",
        memory_namespace="terminalbench.pool",
    )


@pytest.mark.asyncio
async def test_run_trial_forwards_pool_mode_env_when_host_sets_agent_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name, value in _POOL_MODE_HOST_ENV.items():
        monkeypatch.setenv(name, value)
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-pool-1")

    command = plane.host_commands[0]
    for name, value in _POOL_MODE_HOST_ENV.items():
        assert command.environment[name] == value
        assert f"{name}={value}" in command.argv


@pytest.mark.asyncio
async def test_run_trial_task_name_is_request_derived(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODEX_TASK_NAME", "stale-host-name")
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-pool-1")

    command = plane.host_commands[0]
    assert command.environment["MODEX_TASK_NAME"] == "task-pool"
    assert "MODEX_TASK_NAME=task-pool" in command.argv


@pytest.mark.parametrize(
    ("name", "host_value", "container_value"),
    (
        (
            "LANGFUSE_HOST",
            "http://localhost:3000",
            "http://host.docker.internal:3000",
        ),
        (
            "LANGFUSE_HOST",
            "http://lf.example.com:3000",
            "http://lf.example.com:3000",
        ),
        (
            "OTEL_TRACES_ENDPOINT",
            "http://localhost:4318/v1/traces",
            "http://host.docker.internal:4318/v1/traces",
        ),
        (
            "OTEL_TRACES_ENDPOINT",
            "http://host.docker.internal:4318/v1/traces",
            "http://host.docker.internal:4318/v1/traces",
        ),
    ),
)
@pytest.mark.asyncio
async def test_run_trial_containerizes_forwarded_observability_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    host_value: str,
    container_value: str,
) -> None:
    monkeypatch.setenv(name, host_value)
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-url-1")

    command = plane.host_commands[0]
    assert command.environment[name] == container_value
    assert f"{name}={container_value}" in command.argv


@pytest.mark.asyncio
async def test_run_trial_omits_absent_observability_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("OTEL_TRACES_ENDPOINT", raising=False)
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-url-absent")

    command = plane.host_commands[0]
    assert "LANGFUSE_HOST" not in command.environment
    assert "OTEL_TRACES_ENDPOINT" not in command.environment


@pytest.mark.asyncio
async def test_run_trial_without_pool_mode_env_keeps_bare_contract_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in POOL_MODE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-bare-1")

    command = plane.host_commands[0]
    # MODEX_TASK_NAME is request-derived baseline (bare + pool both get it),
    # not a host-forwarded pool-mode option.
    assert command.environment["MODEX_TASK_NAME"] == "task-pool"
    host_forwarded = set(POOL_MODE_ENV_VARS) - {"MODEX_TASK_NAME"}
    assert host_forwarded.isdisjoint(command.environment)
    assert not any(arg.startswith(f"{name}=") for name in host_forwarded for arg in command.argv)


@pytest.mark.asyncio
async def test_run_trial_forwards_uv_python_mirror_when_host_sets_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mirror = "https://mirrors.example/python-build-standalone"
    monkeypatch.setenv(UV_PYTHON_INSTALL_MIRROR_ENV, mirror)
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-mirror-1")

    command = plane.host_commands[0]
    assert command.environment[UV_PYTHON_INSTALL_MIRROR_ENV] == mirror
    assert f"{UV_PYTHON_INSTALL_MIRROR_ENV}={mirror}" in command.argv


@pytest.mark.asyncio
async def test_run_trial_omits_uv_python_mirror_when_host_leaves_it_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(UV_PYTHON_INSTALL_MIRROR_ENV, raising=False)
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))

    await run_trial(_pool_trial_request(tmp_path), plane, lambda _request: "stable-mirror-2")

    command = plane.host_commands[0]
    assert UV_PYTHON_INSTALL_MIRROR_ENV not in command.environment
    assert not any(arg.startswith(f"{UV_PYTHON_INSTALL_MIRROR_ENV}=") for arg in command.argv)


def _write_task_toml(task_dir: Path, text: str) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.toml").write_text(text, encoding="utf-8")
    return task_dir


def test_agent_timeout_multiplier_scales_the_task_budget(tmp_path: Path) -> None:
    budget_900 = _write_task_toml(tmp_path / "task-900", "[agent]\ntimeout_sec = 900\n")
    budget_1800 = _write_task_toml(tmp_path / "task-1800", "[agent]\ntimeout_sec = 1800\n")

    assert _agent_timeout_multiplier(budget_900) == 6.0
    assert _agent_timeout_multiplier(budget_1800) == 3.0


def test_agent_timeout_multiplier_falls_back_to_nominal_900(tmp_path: Path) -> None:
    missing_toml = tmp_path / "task-no-toml"
    missing_toml.mkdir()
    corrupt = _write_task_toml(tmp_path / "task-corrupt", "[agent timeout_sec = oops\n")
    missing_key = _write_task_toml(tmp_path / "task-no-key", "[agent]\nmax_iterations = 5\n")

    for task_dir in (missing_toml, corrupt, missing_key):
        assert _agent_timeout_multiplier(task_dir) == 6.0


@pytest.mark.asyncio
async def test_run_trial_agent_timeout_multiplier_comes_from_task_toml(
    tmp_path: Path,
) -> None:
    task_path = _write_task_toml(tmp_path / "task-flat", "[agent]\ntimeout_sec = 1800\n")
    plane = RecordingPlane(InstallProbeResult(python_available=True, apt_available=False))
    request = RunTrialRequest(
        task_path=task_path,
        jobs_dir=tmp_path / "jobs",
        job_name="flat-one",
        experiment_name="terminalbench.flat",
        dataset_id="dataset-flat",
        item_id="item-flat",
        model="openai/step-3.7-flash",
        memory_namespace="terminalbench.flat",
    )

    await run_trial(request, plane, lambda _request: "stable-flat-1")

    argv = plane.host_commands[0].argv
    assert argv[argv.index("--agent-timeout-multiplier") + 1] == "3.0"
    # the global install/build multiplier stays request-driven, untouched
    assert argv[argv.index("--timeout-multiplier") + 1] == "1.0"


@pytest.mark.asyncio
async def test_subprocess_probe_install_converges_on_probe_install_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exit_by_script = {
        UV_PROBE_COMMAND[-1]: 0,
        PYTHON_PROBE_COMMAND[-1]: 0,
        APT_PROBE_COMMAND[-1]: 0,
    }

    def run(argv, **kwargs):
        _ = kwargs
        return subprocess.CompletedProcess(
            args=argv,
            returncode=exit_by_script.get(argv[-1], 1),
            stdout="",
            stderr="3.11.8",
        )

    monkeypatch.setattr("bot.eval.harbor.host_runtime.subprocess.run", run)

    result = await SubprocessExecutionPlane().probe_install("trial-x")

    assert result == InstallProbeResult(
        python_available=True,
        apt_available=True,
        python_modern=False,
        uv_available=True,
    )


@pytest.mark.asyncio
async def test_subprocess_execution_normalizes_missing_output_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = subprocess.CompletedProcess(
        args=("harbor",), returncode=0, stdout=None, stderr=None
    )
    run = Mock(return_value=completed)
    monkeypatch.setattr(
        "bot.eval.harbor.host_runtime.subprocess.run",
        run,
    )

    result = await SubprocessExecutionPlane().execute_host(HostCommand(argv=("harbor",)))

    assert result == HostCommandResult(exit_code=0, stdout="", stderr="")
    assert run.call_args.kwargs["encoding"] == "utf-8"
    assert run.call_args.kwargs["errors"] == "replace"


def test_preflight_subprocess_uses_utf8_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = subprocess.CompletedProcess(
        args=("docker",),
        returncode=0,
        stdout="\n".join(REQUIRED_LANGFUSE_CONTAINERS),
        stderr="",
    )
    run = Mock(return_value=completed)
    monkeypatch.setattr(smoke_gate.subprocess, "run", run)

    result = run_preflight()

    assert result.missing == ()
    assert all(call.kwargs["encoding"] == "utf-8" for call in run.call_args_list)
    assert all(call.kwargs["errors"] == "replace" for call in run.call_args_list)


def test_collect_maps_trace_artifact_and_official_result_to_contract_score(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "trial-a"
    (trial_dir / "agent").mkdir(parents=True)
    (trial_dir / "agent" / "trace-ids.jsonl").write_text(
        json.dumps({"trace_id": "trace-a", "turn": 1}) + "\n",
        encoding="utf-8",
    )
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "trial-a",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )

    mapping = read_trial_trace_map(tmp_path)
    results = read_official_results(tmp_path)
    specs = collect_verdict_scores(
        mapping,
        results,
        VerdictProvenance(version="terminalbench.official.v1", run_ref="evals/runs/b6"),
    )

    assert mapping == TrialTraceMap(entries=(TrialTrace(trial_id="trial-a", trace_id="trace-a"),))
    assert results == (TerminalBenchResult(trial_id="trial-a", value=1.0),)
    assert len(specs) == 1
    assert specs[0].name == "verdict_terminalbench"
    assert specs[0].data_type == "NUMERIC"
    assert json.loads(specs[0].comment or "") == {
        "scorer": "verifier",
        "version": "terminalbench.official.v1",
        "report_source": "official_harness",
        "run_ref": "evals/runs/b6",
    }


@pytest.mark.parametrize("command", ["install", "run", "judge", "collect"])
def test_typer_exposes_each_host_subcommand(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0


def test_judge_subcommand_reuses_independent_judge_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    execute = Mock()
    monkeypatch.setattr(judge_cli, "_execute_judge_cli", execute)

    result = CliRunner().invoke(app, ["judge", "--experiment", "terminalbench.b6"])

    assert result.exit_code == 0
    assert execute.call_args.args[0].experiment == "terminalbench.b6"


def test_collect_subcommand_uses_injected_collector_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    collect = AsyncMock(return_value=2)
    monkeypatch.setattr(host_cli, "collect_job", collect)

    result = CliRunner().invoke(app, ["collect", "--job-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "collected=2" in result.stdout
    collect.assert_awaited_once_with(
        tmp_path,
        "terminalbench.official.v1",
        tmp_path.as_posix(),
    )


def test_smoke_preflight_checks_docker_and_complete_langfuse_stack() -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...]) -> SmokeCommandResult:
        calls.append(command)
        output = "\n".join(sorted(REQUIRED_LANGFUSE_CONTAINERS))
        return SmokeCommandResult(exit_code=0, stdout=output)

    evidence = run_preflight(run)

    assert evidence.docker_daemon is True
    assert evidence.langfuse_stack is True
    assert evidence.missing == ()
    assert calls == [("docker", "info"), ("docker", "ps", "--format", "{{.Names}}")]


def test_smoke_preflight_reports_missing_daemon_without_second_command() -> None:
    def run(_command: tuple[str, ...]) -> SmokeCommandResult:
        return SmokeCommandResult(exit_code=1, stderr="daemon unavailable")

    evidence = run_preflight(run)

    assert evidence.docker_daemon is False
    assert evidence.langfuse_stack is False
    assert evidence.missing == ("docker-daemon",)


