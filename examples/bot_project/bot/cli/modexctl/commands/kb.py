from __future__ import annotations

import os
from collections.abc import Callable
from typing import Annotated, Any

import httpx
import typer

from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE
from bot.cli.modexctl.context import (
    ModexCtlContext,
    _echo_context_error,
    _missing_comm_env_key,
)
from bot.cli.modexctl.http_client import ControlClientError, get_control_origin
from bot.kb.models import KbAction, KbControlRequest, KbFilter


def _fetch_kb(
    request: KbControlRequest,
    workspace: str,
) -> dict[str, Any]:
    origin = get_control_origin()
    url = f"{origin}/api/control/kb"
    params = {"workspace": workspace} if workspace else None

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=1.0, read=10.0, write=10.0, pool=1.0)
        ) as client:
            response = client.post(
                url,
                json=request.model_dump(mode="json"),
                params=params,
            )
    except httpx.RequestError as exc:
        raise ControlClientError(
            f"Failed to connect to control server at {url}: {exc}"
        ) from exc

    if response.status_code != 200:
        try:
            body = response.json()
            detail = body.get("error", response.text[:200])
        except ValueError:
            detail = response.text[:200]
        raise ControlClientError(
            f"Control server returned HTTP {response.status_code}: {detail}",
            status=response.status_code,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ControlClientError(
            f"Control server returned non-JSON body: {exc}"
        ) from exc


def build_kb_command(ctx: ModexCtlContext) -> Callable[..., None]:
    """Build the persistent knowledge CLI command."""

    def _kb(
        action: Annotated[
            str,
            typer.Argument(help="What to do: search, get, set, delete, or list"),
        ],
        query_or_key: Annotated[
            str | None,
            typer.Argument(
                help="Search query (for search) or key (for get/set/delete)"
            ),
        ] = None,
        value: Annotated[
            str | None,
            typer.Option("--value", "-v", help="Content to store (for set)"),
        ] = None,
        by_task: Annotated[
            str,
            typer.Option(
                "--by-task",
                help=(
                    "Scope to current task (true/false, default: true). "
                    "Set false to search all knowledge regardless of task."
                ),
            ),
        ] = "true",
        category: Annotated[
            str | None,
            typer.Option("--category", help="Filter by category"),
        ] = None,
        limit: Annotated[
            int,
            typer.Option(
                "--limit",
                "-n",
                help="Maximum results (default 20)",
            ),
        ] = 20,
    ) -> None:
        """Search, store, and manage persistent knowledge."""
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        missing_ctx = ctx.validate_history()
        if missing_ctx is not None:
            _echo_context_error(missing_ctx)
            raise typer.Exit(code=EXIT_USAGE)

        try:
            kb_action = KbAction(action)
        except ValueError:
            typer.echo(f"error: invalid action '{action}'", err=True)
            raise typer.Exit(code=EXIT_USAGE) from None

        normalized = by_task.lower()
        if normalized in ("true", "1", "yes", "on"):
            by_task_enabled = True
        elif normalized in ("false", "0", "no", "off"):
            by_task_enabled = False
        else:
            typer.echo(
                f"error: invalid --by-task value '{by_task}'. Use true or false.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)
        task_id = os.environ.get("MODEX_TASK_ID") if by_task_enabled else None
        filter = KbFilter(
            task_id=task_id,
            session_id=ctx.session_id,
            category=category,
        )

        assert ctx.workspace_root is not None
        request = KbControlRequest(
            action=kb_action,
            query_or_key=query_or_key,
            value=value,
            filter=filter,
            limit=limit,
        )
        try:
            result = _fetch_kb(request, ctx.workspace_root)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        typer.echo(result.get("result", ""))

    return _kb
