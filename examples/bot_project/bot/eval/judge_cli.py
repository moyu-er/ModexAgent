"""Typer boundary for standalone experiment re-judging."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer
from langfuse import Langfuse
from langfuse.api.commons.errors.not_found_error import NotFoundError
from pydantic import ValidationError

from bot.eval._judge_pass_models import (
    ExperimentWindow,
    JudgePassConfig,
    JudgePassEnvironment,
)
from bot.eval.judge.runner import (
    JudgeConfigurationError,
    build_judge_provider_from_env,
)


def judge(
    experiment: Annotated[str, typer.Option("--experiment", help="Experiment run name.")],
    rubric_set: Annotated[
        str,
        typer.Option("--rubric-set", help="Central rubric set name."),
    ] = "general-agent",
    dataset: Annotated[
        str | None,
        typer.Option("--dataset", help="Optional dataset used to disambiguate the experiment."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Maximum traces to judge; default is all."),
    ] = None,
    repeats: Annotated[
        int,
        typer.Option(
            "--repeats",
            min=1,
            help="Reviews per trace; agreement is reported and only the first result is injected.",
        ),
    ] = 1,
    archive_root: Annotated[
        Path,
        typer.Option("--archive-root", help="Local root for per-trace judge verdict JSON."),
    ] = Path("evals/runs/judge"),
) -> None:
    """Run an independent judge over already-recorded candidate traces."""
    _execute_judge_cli(
        JudgePassConfig(
            experiment=experiment,
            rubric_set=rubric_set,
            dataset=dataset,
            limit=limit,
            repeats=repeats,
            archive_root=archive_root,
        )
    )


def _execute_judge_cli(config: JudgePassConfig) -> None:
    from bot.eval import cli as eval_cli

    try:
        provider = build_judge_provider_from_env()
    except JudgeConfigurationError as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(code=1) from error

    host, public_key, secret_key = eval_cli._load_langfuse_env()
    dataset_id: str | None = None
    if config.dataset is not None:
        langfuse = Langfuse(
            base_url=host,
            public_key=public_key,
            secret_key=secret_key,
        )
        try:
            dataset_id = langfuse.get_dataset(config.dataset).id
        except NotFoundError:
            typer.echo(
                f"no traces: dataset '{config.dataset}' not found for experiment "
                f"'{config.experiment}'"
            )
            return

    experiments = eval_cli._fetch_experiments(
        host=host,
        headers=eval_cli._basic_auth_header(public_key, secret_key),
        dataset_id=dataset_id,
    )
    matching: list[ExperimentWindow] = []
    for payload in experiments:
        if payload.get("name") != config.experiment:
            continue
        try:
            matching.append(ExperimentWindow.model_validate(payload))
        except ValidationError:
            continue
    if not matching:
        typer.echo(f"no traces: experiment '{config.experiment}' not found")
        return
    if len(matching) > 1:
        typer.echo(
            f"ERROR: experiment '{config.experiment}' is ambiguous; pass --dataset.",
            err=True,
        )
        raise typer.Exit(code=1)

    from bot.eval.judge_pass import run_judge_pass_from_env

    anyio.run(
        run_judge_pass_from_env,
        config,
        matching[0],
        JudgePassEnvironment(
            host=host,
            public_key=public_key,
            secret_key=secret_key,
            provider=provider,
            emit=typer.echo,
        ),
    )
