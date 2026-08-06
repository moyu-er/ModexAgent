"""``modexctl send`` command — send a message to another agent.

Split verbatim from the former ``main.py``; the closure body is unchanged.
The factory wraps it so :class:`~bot.cli.modexctl.context.ModexCtlContext`
is captured exactly as it was when the closure lived inside ``build_app``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from bot.cli.modexctl.ack_adapter import to_agent_send_result
from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE
from bot.cli.modexctl.context import (
    ModexCtlContext,
    _decode_stdin_bytes,
    _echo_context_error,
    _missing_comm_env_key,
    _normalize_text,
)
from bot.cli.modexctl.http_client import ControlClientError, fetch_send
from bot.control.models import AgentSessionRef, SendRequest
from modex_agent.multi_agent.communication.result import format_send_ack


def build_send_command(ctx: ModexCtlContext) -> Callable[..., None]:
    def _send(
        to: Annotated[
            str | None,
            typer.Option(
                "--to",
                help=(
                    "Target agent name. For subagents, defaults to your parent."
                ),
            ),
        ] = None,
        message: Annotated[
            list[str] | None,
            typer.Argument(
                help="Message body as positional args (joined with spaces).",
            ),
        ] = None,
        content: Annotated[
            str | None,
            typer.Option("--content", help="Inline message body."),
        ] = None,
        content_file: Annotated[
            Path | None,
            typer.Option(
                "--content-file", help="Read message body from this UTF-8 file."
            ),
        ] = None,
        use_stdin: Annotated[
            bool,
            typer.Option("--stdin", help="Read message body from stdin."),
        ] = False,
        invocation_id_arg: Annotated[
            str | None,
            typer.Option(
                "--invocation-id", help="Resume a previous invocation."
            ),
        ] = None,
    ) -> None:
        """Send a message to another agent.

        Message input (pick one):
        - Positional args: modexctl send --to agent hello world
        - --content: modexctl send --to agent --content "hello world"
        - --content-file: modexctl send --to agent --content-file msg.txt
        - --stdin: echo "hello" | modexctl send --to agent --stdin
        """
        effective_to = to if to is not None else ctx.default_send_target
        if effective_to is None:
            typer.echo("error: --to is required.", err=True)
            raise typer.Exit(code=EXIT_USAGE)

        has_positional = message is not None and len(message) > 0
        input_modes = sum(
            1
            for x in (
                has_positional,
                content is not None,
                content_file is not None,
                use_stdin,
            )
            if x
        )
        if input_modes == 0:
            typer.echo(
                "error: provide a message. Examples:\n"
                "  modexctl send --to agent hello world\n"
                '  modexctl send --to agent --content "hello world"\n'
                "  modexctl send --to agent --stdin",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)
        if input_modes > 1:
            typer.echo(
                "error: provide only one message source.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        if content_file is not None:
            try:
                content = content_file.read_text(encoding="utf-8")
            except OSError as exc:
                typer.echo(
                    f"error: cannot read --content-file {content_file!s}: {exc}",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE) from None
        elif use_stdin:
            content = _decode_stdin_bytes(sys.stdin.buffer.read())
        elif has_positional:
            content = " ".join(message or [])

        assert content is not None
        content = _normalize_text(content)

        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        pool = ctx.pool_map.get(ctx.agent_name)
        if pool is None:
            typer.echo(
                f"error: agent '{ctx.agent_name}' is not available. "
                "Run 'modexctl agents' to list available agents.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        missing_send = ctx.validate_send()
        if missing_send is not None:
            _echo_context_error(missing_send)
            raise typer.Exit(code=EXIT_USAGE)

        assert ctx.workspace_root is not None
        request = SendRequest(
            caller=AgentSessionRef(
                workspace=Path(ctx.workspace_root),
                pool=pool,
                session_id=ctx.session_id,
                agent_name=ctx.agent_name,
            ),
            comm_kind=ctx.comm_kind,
            parent_session_id=ctx.send_parent_session_id,
            target_agent=effective_to,
            content=content,
            invocation_id=invocation_id_arg.strip() if invocation_id_arg else None,
        )

        try:
            result = fetch_send(request)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        typer.echo(format_send_ack(to_agent_send_result(result)))

    return _send
