"""Manual B6 two-task Harbor-to-Langfuse smoke gate."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypedDict

import anyio
import typer

from bot.eval.harbor.model_source import (
    ModelSource,
    ModelSourceError,
    ResolvedModelSettings,
    inject_model_env,
    resolve_model_settings,
)
from bot.eval.harbor.pool_mode_types import DEFAULT_POOL_NAME
from bot.eval.harbor.smoke_evidence import (
    REQUIRED_LANGFUSE_CONTAINERS,
    B6PoolSmokeEvidence,
    B6SmokeEvidence,
    SmokeCommandResult,
    SmokePreflight,
    SmokeRequest,
    missing_pool_env,
    pool_budget_usd,
)
from bot.eval.harbor.smoke_runtime import (
    SmokeGateError,
    SmokeMode,
    SmokeRunRequest,
    run_smoke,
)
from modex_agent.core.constants import ReasoningEffort

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)

_EVIDENCE_PATH = Path("evals/evidence/b6_smoke.json")
_POOL_EVIDENCE_PATH = Path("evals/evidence/b6_pool_smoke.json")

type SmokeCommandRunner = Callable[[tuple[str, ...]], SmokeCommandResult]


def _run_command(command: tuple[str, ...]) -> SmokeCommandResult:
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=5,
    )
    return SmokeCommandResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_preflight(
    run: SmokeCommandRunner = _run_command,
    *,
    mode: SmokeMode = SmokeMode.BARE,
) -> SmokePreflight:
    docker = run(("docker", "info"))
    if docker.exit_code != 0:
        return SmokePreflight(
            docker_daemon=False,
            langfuse_stack=False,
            missing=("docker-daemon",),
        )
    running = run(("docker", "ps", "--format", "{{.Names}}"))
    names = frozenset(running.stdout.splitlines()) if running.exit_code == 0 else frozenset()
    missing = tuple(f"container:{name}" for name in sorted(REQUIRED_LANGFUSE_CONTAINERS - names))
    if mode is SmokeMode.POOL:
        missing += missing_pool_env()
    return SmokePreflight(
        docker_daemon=True,
        langfuse_stack=not any(item.startswith("container:") for item in missing),
        missing=missing,
    )


class _ModelEvidenceFields(TypedDict):
    model: str | None
    model_source: ModelSource | None
    temperature: float | None
    reasoning_effort: ReasoningEffort | None


def _model_evidence_fields(
    resolved: ResolvedModelSettings | None,
) -> _ModelEvidenceFields:
    if resolved is None:
        return {"model": None, "model_source": None, "temperature": None, "reasoning_effort": None}
    return {
        "model": resolved.model,
        "model_source": resolved.source,
        "temperature": resolved.temperature,
        "reasoning_effort": resolved.reasoning_effort,
    }


class _PoolEvidenceBase(_ModelEvidenceFields):
    checked_at: datetime
    preflight: SmokePreflight
    run_id: str
    task_paths: tuple[str, ...]
    pool_name: str
    budget_usd: float


async def _run_pool_gate(
    request: SmokeRequest,
    checked_at: datetime,
    preflight: SmokePreflight,
    resolved: ResolvedModelSettings | None,
) -> B6PoolSmokeEvidence:
    experiment_name = f"terminalbench.{request.run_id}"
    base: _PoolEvidenceBase = {
        **_model_evidence_fields(resolved),
        "checked_at": checked_at,
        "preflight": preflight,
        "run_id": request.run_id,
        "task_paths": tuple(path.as_posix() for path in request.task_paths),
        "pool_name": os.environ.get("MODEX_POOL_NAME") or DEFAULT_POOL_NAME,
        "budget_usd": pool_budget_usd(),
    }
    if preflight.missing:
        return B6PoolSmokeEvidence(
            passed=False,
            experiment_name=experiment_name,
            error="preflight failed: " + ", ".join(preflight.missing),
            **base,
        )
    assert resolved is not None  # resolution failures are folded into preflight.missing above
    try:
        result = await run_smoke(
            SmokeRunRequest(
                task_paths=request.task_paths,
                run_id=request.run_id,
                model=resolved.model,
                timeout_multiplier=request.timeout_multiplier,
                jobs_dir=request.jobs_dir,
                mode=SmokeMode.POOL,
            )
        )
        return B6PoolSmokeEvidence(
            passed=True,
            experiment_name=result.experiment_name,
            job_dirs=result.job_dirs,
            trace_ids=result.trace_ids,
            verdicts=result.verdicts,
            score_count=result.score_count,
            install_seconds=result.install_seconds,
            child_sessions=result.child_sessions,
            delegation_counts=result.delegation_counts,
            **base,
        )
    except SmokeGateError as error:
        return B6PoolSmokeEvidence(
            passed=False,
            experiment_name=experiment_name,
            error=str(error),
            **base,
        )


def _resolve_request_model(
    request: SmokeRequest,
) -> tuple[SmokeRequest, ResolvedModelSettings | None, str | None]:
    try:
        settings = resolve_model_settings(request.model, request.model_yml)
    except ModelSourceError as error:
        return request, None, str(error)
    inject_model_env(settings)
    return request.model_copy(update={"model": settings.model}), settings, None


async def run_gate(
    request: SmokeRequest,
) -> B6SmokeEvidence | B6PoolSmokeEvidence:
    """Run exactly two local Harbor tasks and persist bounded B6 evidence."""
    checked_at = datetime.now(UTC)
    request, resolved, model_error = _resolve_request_model(request)
    preflight = run_preflight(mode=request.mode)
    if model_error is not None:
        preflight = preflight.model_copy(
            update={"missing": (*preflight.missing, f"model source: {model_error}")}
        )
    evidence: B6SmokeEvidence | B6PoolSmokeEvidence
    if request.mode is SmokeMode.POOL:
        evidence = await _run_pool_gate(request, checked_at, preflight, resolved)
        request.evidence_path.parent.mkdir(parents=True, exist_ok=True)
        request.evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
        return evidence
    experiment_name = f"terminalbench.{request.run_id}"
    evidence = B6SmokeEvidence(
        passed=False,
        checked_at=checked_at,
        preflight=preflight,
        run_id=request.run_id,
        experiment_name=experiment_name,
        task_paths=tuple(path.as_posix() for path in request.task_paths),
        error="preflight failed: " + ", ".join(preflight.missing),
        **_model_evidence_fields(resolved),
    )
    if not preflight.missing:
        assert resolved is not None  # resolution failures are folded into preflight.missing above
        try:
            result = await run_smoke(
                SmokeRunRequest(
                    task_paths=request.task_paths,
                    run_id=request.run_id,
                    model=resolved.model,
                    timeout_multiplier=request.timeout_multiplier,
                    jobs_dir=request.jobs_dir,
                )
            )
            evidence = B6SmokeEvidence(
                passed=True,
                checked_at=checked_at,
                preflight=preflight,
                run_id=request.run_id,
                experiment_name=result.experiment_name,
                task_paths=tuple(path.as_posix() for path in request.task_paths),
                job_dirs=result.job_dirs,
                trace_ids=result.trace_ids,
                verdicts=result.verdicts,
                score_count=result.score_count,
                **_model_evidence_fields(resolved),
            )
        except SmokeGateError as error:
            evidence = evidence.model_copy(update={"error": str(error)})
    request.evidence_path.parent.mkdir(parents=True, exist_ok=True)
    request.evidence_path.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
    return evidence


@app.command()
def main(
    task_path: Annotated[list[Path], typer.Option("--task-path")],
    run_id: Annotated[str, typer.Option("--run-id")],
    model: Annotated[str | None, typer.Option("--model")] = None,
    timeout_multiplier: Annotated[float, typer.Option("--timeout-multiplier", min=0.01)] = 6.0,
    jobs_dir: Annotated[Path, typer.Option("--jobs-dir")] = Path("evals/runs/harbor"),
    mode: Annotated[SmokeMode, typer.Option("--mode")] = SmokeMode.BARE,
    model_yml: Annotated[Path | None, typer.Option("--model-yml")] = None,
) -> None:
    """Dispatch the manual B6 gate; Docker and two task paths are required."""
    if len(task_path) != 2:
        typer.echo("ERROR: pass exactly two --task-path values", err=True)
        raise typer.Exit(code=2)
    evidence_path = _POOL_EVIDENCE_PATH if mode is SmokeMode.POOL else _EVIDENCE_PATH
    evidence = anyio.run(
        run_gate,
        SmokeRequest(
            task_paths=(task_path[0], task_path[1]),
            run_id=run_id,
            model=model,
            timeout_multiplier=timeout_multiplier,
            jobs_dir=jobs_dir,
            mode=mode,
            evidence_path=evidence_path,
            model_yml=model_yml,
        ),
    )
    typer.echo(evidence.model_dump_json(indent=2))
    if not evidence.passed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
