"""``modexctl agents`` command — list available agents.

Split verbatim from the former ``main.py``; the closure body is unchanged.
The factory wraps it so :class:`~bot.cli.modexctl.context.ModexCtlContext`
is captured exactly as it was when the closure lived inside ``build_app``.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from bot.cli.modexctl.app import EXIT_USAGE
from bot.cli.modexctl.context import (
    ModexCtlContext,
    _echo_context_error,
    _missing_comm_env_key,
)


def build_agents_command(ctx: ModexCtlContext) -> Callable[[], None]:
    def _agents() -> None:
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        targets = ctx.visible_targets
        if not targets:
            typer.echo("No available agents configured.")
            return
        kinds = ctx.target_kinds
        max_name_len = max(len(name) for name in targets)

        if ctx.is_subagent:
            typer.echo("Your only contact is the agent that assigned you this task:")
            for name, description in targets.items():
                typer.echo(f"  {name:<{max_name_len}}  {description}")
            return

        typer.echo("Available agents (use the exact name with 'modexctl send --to'):")
        typer.echo()
        typer.echo("  (subagent): your helper — delegate a self-contained task.")
        typer.echo("    Use --invocation-id to continue a previous task (null starts new).")
        typer.echo("  (normal): an independent agent in another team — communicate as equals.")
        typer.echo("    --invocation-id is ignored; each conversation keeps a stable thread.")
        typer.echo()
        for name, description in targets.items():
            kind = kinds.get(name, "normal")
            typer.echo(f"  {name:<{max_name_len}}  {description} ({kind})")

    return _agents
