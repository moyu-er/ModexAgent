"""Injected host execution plane for Harbor installation and trial launch."""

from __future__ import annotations

import os
import subprocess
import tomllib
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Final

from anyio import to_thread
from pydantic import BaseModel, ConfigDict, Field

from bot.eval.evalenv import LangfuseCredentials
from bot.eval.harbor.agent import (
    POOL_MODE_ENV_VARS,
    SOURCE_TAR_CONTAINER_PATH,
    UV_PYTHON_INSTALL_MIRROR_ENV,
    CommandExecution,
    CommandResult,
    HarborTaskResult,
    InstallExecutionResult,
    InstallProbeResult,
    InstallSettings,
    build_install_plan,
    execute_install_plan,
    probe_install_runtime,
)
from bot.eval.harbor.source_package import SourceArchive, build_source_archive
from modex_agent.trace.experiment_attrs import stable_experiment_id

_REPO_ROOT: Final = Path(__file__).resolve().parents[5]
_BOT_PROJECT: Final = _REPO_ROOT / "examples" / "bot_project"
# Anchored to __file__, never CWD-relative: trial launch must work from any
# working directory (CI pytest runs from the repo root).
DEFAULT_COMPOSE_OVERLAY: Final = Path(__file__).resolve().parent / "docker-compose.uv.yml"


class HostCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(min_length=1)
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int | None = Field(default=None, gt=0)


class HostCommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""


class HostInstallRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: Path
    archive_path: Path
    container: str = Field(min_length=1)
    settings: InstallSettings = Field(default_factory=InstallSettings)


class HostInstallResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    archive: SourceArchive | None = None
    execution: InstallExecutionResult

    @property
    def task_result(self) -> HarborTaskResult:
        return self.execution.task_result

    @property
    def include_in_aggregate(self) -> bool:
        return self.execution.include_in_aggregate


class RunTrialRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_path: Path
    jobs_dir: Path
    job_name: str = Field(min_length=1)
    experiment_name: str = Field(min_length=1)
    dataset_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    memory_namespace: str = Field(min_length=1)
    timeout_multiplier: float = Field(default=1.0, gt=0)
    compose_overlay: Path | None = None


class RunTrialResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    command: HostCommand
    result: HostCommandResult


class HostExecutionPlane(ABC):
    """Narrow Docker/host process surface replaced by fakes in unit tests."""

    @abstractmethod
    async def probe_install(self, container: str) -> InstallProbeResult:
        pass

    @abstractmethod
    async def upload_file(self, container: str, source: Path, target: str) -> None:
        pass

    @abstractmethod
    async def execute_install(
        self,
        container: str,
        execution: CommandExecution,
    ) -> CommandResult:
        pass

    @abstractmethod
    async def execute_host(self, command: HostCommand) -> HostCommandResult:
        pass


type StableIdFactory = Callable[[RunTrialRequest], str]


async def install_host(
    request: HostInstallRequest,
    execution: HostExecutionPlane,
) -> HostInstallResult:
    """Probe, package, upload, and execute the converged T26 install plan."""
    probe = await execution.probe_install(request.container)
    plan = build_install_plan(probe, request.settings)
    if not plan.include_in_aggregate:
        result = await execute_install_plan(plan, lambda _command: _unreachable_command())
        return HostInstallResult(execution=result)
    archive = build_source_archive(request.repo_root, request.archive_path)
    await execution.upload_file(request.container, archive.path, SOURCE_TAR_CONTAINER_PATH)

    async def execute(command: CommandExecution) -> CommandResult:
        return await execution.execute_install(request.container, command)

    result = await execute_install_plan(plan, execute)
    return HostInstallResult(archive=archive, execution=result)


async def _unreachable_command() -> CommandResult:
    raise AssertionError("skipped install plan attempted command execution")


def _agent_timeout_multiplier(task_path: Path) -> float:
    """5400s flat cap per task: multiplier = 5400 / the task's nominal agent budget."""
    try:
        data = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8"))
        nominal = float(data.get("agent", {}).get("timeout_sec", 900.0))
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        nominal = 900.0
    return round(5400.0 / max(nominal, 1.0), 4)


def _materialize_overlay(overlay: Path, jobs_dir: Path, job_name: str) -> Path:
    """Render the compose overlay with bind-mount sources anchored to the
    overlay's own directory.

    Harbor runs compose with ``--project-directory <task environment dir>``,
    so relative bind mounts in the overlay resolve against the TASK directory,
    not the overlay's location — the python-runtimes/ mount would point at a
    nonexistent path and the seed container would find an empty /runtimes.
    Rendering a per-trial copy with absolute sources (resolved from the
    overlay's real parent, so nothing is hard-coded) keeps the overlay
    portable across checkouts. The copy is per-job: jobs_dir is shared by
    all concurrent trials, and a fixed name let 8 trials rewrite one file
    mid-flight — a compose read during a rewrite saw config-hash drift for
    the running main service and recreated the container, wiping the
    agent's /app writes before the verifier ran (six "does not exist"
    false failures in tb21-all-v7).
    """
    overlay_path = overlay.resolve()
    rendered = overlay_path.read_text(encoding="utf-8").replace(
        "./python-runtimes",
        (overlay_path.parent / "python-runtimes").as_posix(),
    )
    # Per-trial filename: jobs_dir is SHARED by all concurrent trials — one
    # fixed name meant 8 trials rewrote the same file mid-flight, and any
    # compose invocation that read it during (or after) a rewrite saw a
    # config-hash drift for the running main service, recreating the
    # container and wiping the agent's /app writes before the verifier ran
    # (observed: six "does not exist" false failures in tb21-all-v7).
    target = jobs_dir / f"{job_name}__docker-compose.uv.rendered.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    return target


async def run_trial(
    request: RunTrialRequest,
    execution: HostExecutionPlane,
    mint_experiment_id: StableIdFactory,
) -> RunTrialResult:
    """Mint experiment identity, inject T27 env, and launch one Harbor trial."""
    experiment_id = mint_experiment_id(request)
    environment = _trial_environment(request, experiment_id)
    argv = [
        "harbor",
        "run",
        "--path",
        str(request.task_path),
        "--agent",
        "bot.eval.harbor.installed_agent:ModexHarborAgent",
        "--model",
        request.model,
        "--job-name",
        request.job_name,
        "--jobs-dir",
        str(request.jobs_dir),
        "--n-concurrent",
        "1",
        "--timeout-multiplier",
        str(request.timeout_multiplier),
        # Agent-phase wall clock: 5400s flat cap per task — the multiplier
        # scales the task's own task.toml [agent].timeout_sec budget (900s
        # nominal); install/build keep the generous global multiplier.
        "--agent-timeout-multiplier",
        str(_agent_timeout_multiplier(request.task_path)),
        # Full compose teardown after each trial (down --volumes --remove-orphans):
        # without it, plain `down` leaves the modex-uv-bin overlay volume behind,
        # and killed subprocesses leak containers+networks (Windows network cap ~31).
        "--delete",
        "--yes",
    ]
    if request.compose_overlay is not None:
        argv.extend(
            (
                "--extra-docker-compose",
                str(_materialize_overlay(request.compose_overlay, request.jobs_dir, request.job_name)),
            )
        )
    for name, value in sorted(environment.items()):
        argv.extend(("--agent-env", f"{name}={value}"))
    command = HostCommand(argv=tuple(argv), environment=environment)
    result = await execution.execute_host(command)
    return RunTrialResult(experiment_id=experiment_id, command=command, result=result)


def mint_stable_experiment_id(request: RunTrialRequest) -> str:
    """Mint T14's stable ID from the host process environment."""
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise KeyError("Langfuse credentials are required")
    host = credentials.host if credentials.host is not None else "http://localhost:3000"
    return stable_experiment_id(
        host=host,
        public_key=credentials.public_key,
        secret_key=credentials.secret_key,
        dataset_id=request.dataset_id,
        item_id=request.item_id,
        run_name=request.experiment_name,
    )


def _containerize_url(raw: str | None) -> str | None:
    """Rewrite host loopback URLs because containers cannot reach the host's localhost."""
    if not raw:
        return raw
    return raw.replace("://localhost:", "://host.docker.internal:").replace(
        "://127.0.0.1:", "://host.docker.internal:"
    )


def _trial_environment(request: RunTrialRequest, experiment_id: str) -> dict[str, str]:
    python_path = os.pathsep.join(
        (
            str(_REPO_ROOT / "src"),
            str(_BOT_PROJECT),
            os.environ.get("PYTHONPATH", ""),
        )
    ).strip(os.pathsep)
    environment = {
        "LLM_MODEL": request.model,
        "MODEX_EXPERIMENT_ID": experiment_id,
        "MODEX_EXPERIMENT_NAME": request.experiment_name,
        "MODEX_EXPERIMENT_DATASET_ID": request.dataset_id,
        "MODEX_EXPERIMENT_ITEM_ID": request.item_id,
        "MODEX_MEMORY_NS": request.memory_namespace,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": python_path,
        "PYTHONUTF8": "1",
    }
    for name in ("OTEL_TRACES_ENDPOINT", "LANGFUSE_HOST"):
        if value := _containerize_url(os.environ.get(name)):
            environment[name] = value
    for name in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LANGFUSE_BASIC_AUTH",
        "MODEX_PIP_INDEX",
        UV_PYTHON_INSTALL_MIRROR_ENV,
        *POOL_MODE_ENV_VARS,
    ):
        if value := os.environ.get(name):
            environment[name] = value
    # Task identity is request-derived, so it wins over any forwarded host
    # MODEX_TASK_NAME picked up by the loop above.
    environment["MODEX_TASK_NAME"] = request.task_path.name
    return environment


class SubprocessExecutionPlane(HostExecutionPlane):
    """Real Docker/Harbor execution plane used only by manual CLI dispatch."""

    async def probe_install(self, container: str) -> InstallProbeResult:
        async def probe(argv: tuple[str, ...]) -> bool:
            result = await self.execute_host(HostCommand(argv=("docker", "exec", container, *argv)))
            return result.exit_code == 0

        return await probe_install_runtime(probe)

    async def upload_file(self, container: str, source: Path, target: str) -> None:
        result = await self.execute_host(
            HostCommand(argv=("docker", "cp", str(source), f"{container}:{target}"))
        )
        if result.exit_code != 0:
            raise RuntimeError(result.stderr)

    async def execute_install(self, container: str, execution: CommandExecution) -> CommandResult:
        argv = ["docker", "exec", "--user", "root"]
        for name, value in execution.environment.items():
            argv.extend(("--env", f"{name}={value}"))
        argv.extend((container, *execution.command.argv))
        result = await self.execute_host(
            HostCommand(argv=tuple(argv), timeout_seconds=execution.timeout_seconds)
        )
        return CommandResult(exit_code=result.exit_code, stderr=result.stderr)

    async def execute_host(self, command: HostCommand) -> HostCommandResult:
        def run_command() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                command.argv,
                capture_output=True,
                check=False,
                encoding="utf-8",
                env={**os.environ, **command.environment},
                errors="replace",
                text=True,
                timeout=command.timeout_seconds,
            )

        completed = await to_thread.run_sync(run_command)
        return HostCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )


__all__ = [
    "HostCommand",
    "HostCommandResult",
    "HostExecutionPlane",
    "HostInstallRequest",
    "HostInstallResult",
    "RunTrialRequest",
    "RunTrialResult",
    "StableIdFactory",
    "SubprocessExecutionPlane",
    "install_host",
    "mint_stable_experiment_id",
    "run_trial",
]

