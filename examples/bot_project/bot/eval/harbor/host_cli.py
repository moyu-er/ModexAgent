"""Standalone Harbor host CLI for installation, trials, judging, and verdicts."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Annotated, Final

import anyio
import typer

from bot.eval.evalenv import LangfuseCredentials
from bot.eval.harbor.agent import InstallSettings
from bot.eval.harbor.host_runtime import (
    HostCommand,
    HostCommandResult,
    HostExecutionPlane,
    HostInstallRequest,
    HostInstallResult,
    RunTrialRequest,
    RunTrialResult,
    SubprocessExecutionPlane,
    install_host,
    mint_stable_experiment_id,
    run_trial,
)
from bot.eval.harbor.verdict_collector import (
    VerdictProvenance,
    inject_verdict_scores,
    read_official_results,
    read_trial_trace_map,
)
from bot.eval.judge_cli import judge
from modex_agent.trace.score_injector import L2ScoreInjector

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)
app.command("judge")(judge)

_REPO_ROOT: Final = Path(__file__).resolve().parents[5]
_DEFAULT_ARCHIVE: Final = Path("evals/runs/harbor/modex-src.tar.gz")
_DEFAULT_OVERLAY: Final = Path("bot/eval/harbor/docker-compose.uv.yml")


@app.command("install")
def install_command(
    container: Annotated[str, typer.Option("--container")],
    repo_root: Annotated[Path, typer.Option("--repo-root")] = _REPO_ROOT,
    archive_path: Annotated[Path, typer.Option("--archive-path")] = _DEFAULT_ARCHIVE,
) -> None:
    """Install the T26 source package into an already-running task container."""
    result = anyio.run(
        install_host,
        HostInstallRequest(
            repo_root=repo_root,
            archive_path=archive_path,
            container=container,
            settings=InstallSettings.from_environment(os.environ),
        ),
        SubprocessExecutionPlane(),
    )
    typer.echo(result.model_dump_json(indent=2))
    if not result.include_in_aggregate:
        raise typer.Exit(code=2)


@app.command("run")
def run_command(
    task_path: Annotated[Path, typer.Option("--task-path")],
    job_name: Annotated[str, typer.Option("--job-name")],
    experiment_name: Annotated[str, typer.Option("--experiment")],
    dataset_id: Annotated[str, typer.Option("--dataset-id")],
    item_id: Annotated[str, typer.Option("--item-id")],
    model: Annotated[str, typer.Option("--model")],
    memory_namespace: Annotated[str, typer.Option("--memory-namespace")],
    jobs_dir: Annotated[Path, typer.Option("--jobs-dir")] = Path("evals/runs/harbor"),
    timeout_multiplier: Annotated[
        float,
        typer.Option("--timeout-multiplier", min=0.01),
    ] = 1.0,
    compose_overlay: Annotated[
        Path | None,
        typer.Option("--compose-overlay"),
    ] = _DEFAULT_OVERLAY,
) -> None:
    """Launch one local Harbor task with T14/T27 experiment environment."""
    request = RunTrialRequest(
        task_path=task_path,
        jobs_dir=jobs_dir,
        job_name=job_name,
        experiment_name=experiment_name,
        dataset_id=dataset_id,
        item_id=item_id,
        model=model,
        memory_namespace=memory_namespace,
        timeout_multiplier=timeout_multiplier,
        compose_overlay=compose_overlay,
    )
    result = anyio.run(
        run_trial,
        request,
        SubprocessExecutionPlane(),
        mint_stable_experiment_id,
    )
    typer.echo(result.model_dump_json(indent=2))
    if result.result.exit_code != 0:
        raise typer.Exit(code=result.result.exit_code)


async def collect_job(
    job_dir: Path,
    version: str,
    run_ref: str,
) -> int:
    """Read one Harbor job and inject official verdicts onto mapped traces."""
    mapping = read_trial_trace_map(job_dir)
    results = read_official_results(job_dir)
    credentials = LangfuseCredentials.from_env()
    host = (
        credentials.host
        if credentials is not None and credentials.host is not None
        else os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    ).rstrip("/")
    basic_auth = os.environ.get("LANGFUSE_BASIC_AUTH")
    if basic_auth is None:
        if credentials is None:
            raise KeyError("Langfuse credentials are required")
        credential_pair = f"{credentials.public_key}:{credentials.secret_key}"
        basic_auth = base64.b64encode(credential_pair.encode()).decode("ascii")
    injector = L2ScoreInjector(
        ingestion_url=f"{host}/api/public/ingestion",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "x-langfuse-ingestion-version": "4",
        },
    )
    try:
        await inject_verdict_scores(
            mapping,
            results,
            VerdictProvenance(version=version, run_ref=run_ref),
            injector.inject_score_batch,
        )
    finally:
        await injector.aclose()
    return len(results)


@app.command("collect")
def collect_command(
    job_dir: Annotated[Path, typer.Option("--job-dir")],
    version: Annotated[str, typer.Option("--version")] = "terminalbench.official.v1",
    run_ref: Annotated[str | None, typer.Option("--run-ref")] = None,
) -> None:
    """Collect official Harbor results as ``verdict_terminalbench`` scores."""
    resolved_run_ref = run_ref or job_dir.as_posix()
    count = anyio.run(collect_job, job_dir, version, resolved_run_ref)
    typer.echo(f"collected={count} run_ref={resolved_run_ref}")


if __name__ == "__main__":
    app()


__all__ = [
    "HostCommand",
    "HostCommandResult",
    "HostExecutionPlane",
    "HostInstallRequest",
    "HostInstallResult",
    "RunTrialRequest",
    "RunTrialResult",
    "app",
    "collect_job",
    "install_host",
    "run_trial",
]
