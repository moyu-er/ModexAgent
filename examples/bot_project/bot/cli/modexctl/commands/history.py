"""``modexctl history`` command — list agent message history.

Split verbatim from the former ``main.py``; the closure body is unchanged.
The factory wraps it so :class:`~bot.cli.modexctl.context.ModexCtlContext`
is captured exactly as it was when the closure lived inside ``build_app``.

``_format_send_ack`` (previously co-located here) moved to
:mod:`bot.cli.modexctl.app` — it is shared by both ``send`` and
``history`` commands and belongs in the common app module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE
from bot.cli.modexctl.context import (
    ModexCtlContext,
    _echo_context_error,
    _missing_comm_env_key,
)
from bot.cli.modexctl.http_client import ControlClientError, fetch_history
from bot.control.models import (
    AgentSessionRef,
    HistoryMessage,
    HistoryRequest,
)

# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

_HISTORY_MAX_LIMIT: int = 10

_CLI_HISTORY_FIELDS: frozenset[str] = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "tool_name",
        "name",
        "created_at",
        "message_id",
    }
)


def _clamp_limit(limit: int) -> int:
    if limit > _HISTORY_MAX_LIMIT:
        return _HISTORY_MAX_LIMIT
    return limit


def _project_client_message(msg: HistoryMessage) -> dict[str, Any]:
    raw = msg.model_dump(exclude_none=True)
    return {k: v for k, v in raw.items() if k in _CLI_HISTORY_FIELDS}


def _format_jsonl(items: list[HistoryMessage]) -> list[str]:
    return [
        json.dumps(_project_client_message(m), ensure_ascii=False) for m in items
    ]


# ---------------------------------------------------------------------------
# history command factory
# ---------------------------------------------------------------------------


def build_history_command(ctx: ModexCtlContext) -> Callable[..., None]:
    def _history(
        agent: Annotated[
            str | None,
            typer.Option(
                "--agent",
                help="Subagent name. Only valid with --invocation-id. "
                "Must be a (subagent) from 'modexctl agents', not a (normal).",
            ),
        ] = None,
        invocation_id: Annotated[
            str | None,
            typer.Option(
                "--invocation-id",
                help="Task invocation id from the send ack when you dispatched "
                "the task. Only valid with --agent.",
            ),
        ] = None,
        limit: Annotated[
            int,
            typer.Option("--limit", help="Number of messages (default 3, max 10)."),
        ] = 3,
    ) -> None:
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        if limit <= 0:
            typer.echo("error: --limit must be positive", err=True)
            raise typer.Exit(code=EXIT_USAGE)
        clamped = _clamp_limit(limit)

        if agent is None or agent == ctx.agent_name:
            session_id = ctx.session_id
            target_agent = ctx.agent_name
        else:
            kind = ctx.target_kinds.get(agent)
            if kind is None:
                typer.echo(
                    f"error: agent '{agent}' is not available. "
                    "Run 'modexctl agents' to list available agents.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE)
            if kind != "subagent":
                typer.echo(
                    f"error: cannot read history of '{agent}' ({kind}). "
                    "You can only read your own history and your subagents'.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE)
            if not invocation_id:
                typer.echo(
                    f"error: --invocation-id is required to read subagent '{agent}' history. "
                    "It comes from the send ack when you dispatched the task.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE)
            session_id = f"{invocation_id}.{agent}"
            target_agent = agent

        pool = ctx.pool_map.get(target_agent)
        if pool is None:
            typer.echo(
                f"error: agent '{target_agent}' is not available.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        missing_hist = ctx.validate_history()
        if missing_hist is not None:
            _echo_context_error(missing_hist)
            raise typer.Exit(code=EXIT_USAGE)

        assert ctx.workspace_root is not None
        request = HistoryRequest(
            caller=AgentSessionRef(
                workspace=Path(ctx.workspace_root),
                pool=pool,
                session_id=session_id,
                agent_name=target_agent,
            ),
            limit=clamped,
        )

        try:
            result = fetch_history(request)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        if not result.items:
            typer.echo("No history found.", err=True)
            return

        typer.echo("# History below is ordered newest-first (top = most recent).")
        for line in _format_jsonl(result.items):
            typer.echo(line)

    return _history
