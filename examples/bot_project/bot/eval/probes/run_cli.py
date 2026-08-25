"""Typer entry point for the manual B5 memory-probe dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import anyio
import typer
from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.dispatch import (
    ProbeDispatchError,
    ProbeDispatchOptions,
)
from bot.eval.probes.dispatch import dispatch_probe_run as _dispatch_probe_run

app = typer.Typer(add_completion=False, no_args_is_help=True)


class ProbeCliOptions(BaseModel):
    """Required command-line values before live environment resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library: Path
    manifest: Path
    run_name: str = Field(min_length=1)
    max_cost_usd: float = Field(gt=0)


async def dispatch_probe_run(options: ProbeCliOptions) -> Path:
    """Delegate parsed CLI values to the live dispatch boundary."""
    return await _dispatch_probe_run(
        ProbeDispatchOptions(
            library=options.library,
            manifest=options.manifest,
            run_name=options.run_name,
            max_cost_usd=options.max_cost_usd,
        )
    )


@app.command()
def main(
    library: Annotated[Path, typer.Option("--library", exists=True, dir_okay=False)],
    manifest: Annotated[Path, typer.Option("--manifest", exists=True, dir_okay=False)],
    run_name: Annotated[str, typer.Option("--run-name")],
    max_cost_usd: Annotated[float, typer.Option("--max-cost", min=0.000001)],
) -> None:
    """Dispatch a frozen probe library and write the fixed B5 evidence receipt."""
    options = ProbeCliOptions(
        library=library,
        manifest=manifest,
        run_name=run_name,
        max_cost_usd=max_cost_usd,
    )
    try:
        evidence_path = anyio.run(dispatch_probe_run, options)
    except ProbeDispatchError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Evidence: {evidence_path}")


if __name__ == "__main__":
    app()
