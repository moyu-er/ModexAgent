"""``modexctl`` app factory + entry point + shared formatting helpers.

Holds :func:`build_app` (which wires the three command closures from
:mod:`bot.cli.modexctl.commands` into a Typer app) and :func:`main` (the
console-script entry point re-exported by ``__init__``). Send-ack formatting
is handled by the framework's :func:`format_send_ack` via the adapter in
:mod:`bot.cli.modexctl.ack_adapter`.
"""

from __future__ import annotations

import sys

import typer

from bot.cli.modexctl.context import (
    ModexCtlContext,
    _missing_comm_env_key,
    _missing_workflow_env_key,
)

# ---------------------------------------------------------------------------
# Exit codes — module-level so subprocess tests can reference them
# ---------------------------------------------------------------------------

EXIT_OK: int = 0
EXIT_USAGE: int = 1
EXIT_ROUTING: int = 2


# ---------------------------------------------------------------------------
# Workflow stub
# ---------------------------------------------------------------------------


def _stub_workflow_command() -> None:
    typer.echo("workflow commands are not available.")


# ---------------------------------------------------------------------------
# App factory — builds commands as closures capturing ModexCtlContext
# ---------------------------------------------------------------------------


def _build_app_help() -> str:
    if _missing_comm_env_key() is not None:
        return (
            "modexctl — ModexBot control CLI.\n\n"
            "This CLI is available only within a running ModexBot context.\n"
            "No commands are available in the current environment."
        )
    return "modexctl — ModexBot control CLI."


def _noop_callback() -> None:
    pass


def build_app() -> typer.Typer:
    from bot.cli.modexctl.commands.agents import build_agents_command
    from bot.cli.modexctl.commands.history import build_history_command
    from bot.cli.modexctl.commands.send import build_send_command

    app = typer.Typer(
        name="modexctl",
        help=_build_app_help(),
        invoke_without_command=True,
        no_args_is_help=True,
        add_completion=False,
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
    )
    app.callback()(_noop_callback)

    ctx = ModexCtlContext.from_env()
    if ctx is None:
        return app

    # -- agents ---------------------------------------------------------------

    app.command(name="agents", help="List available agents.")(
        build_agents_command(ctx)
    )

    # -- send -----------------------------------------------------------------

    app.command(name="send", help="Send a message to another agent.")(
        build_send_command(ctx)
    )

    # -- history --------------------------------------------------------------

    if ctx.is_subagent:
        history_help = "Read your own session message history as JSON Lines."
    else:
        history_help = (
            "Read session message history as JSON Lines.\n\n"
            "You can read two kinds of history:\n"
            "  1. Your own session (omit both --agent and --invocation-id).\n"
            "  2. A subagent task you dispatched (provide both --agent and\n"
            "     --invocation-id from that task's send ack).\n\n"
            "You cannot read other teams' (normal) agents' history —\n"
            "only your own and your subagents'."
        )

    app.command(name="history", help=history_help)(build_history_command(ctx))

    # -- workflow stubs -------------------------------------------------------

    if _missing_workflow_env_key() is None:
        app.command(name="submit")(_stub_workflow_command)
        app.command(name="next-steps")(_stub_workflow_command)
        app.command(name="task")(_stub_workflow_command)
        app.command(name="workflow")(_stub_workflow_command)

    return app


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def main() -> None:
    import io
    from typing import cast

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            cast("io.TextIOWrapper", stream).reconfigure(
                encoding="utf-8", errors="replace"
            )

    app = build_app()
    app()


if __name__ == "__main__":
    main()
    sys.exit(EXIT_OK)
