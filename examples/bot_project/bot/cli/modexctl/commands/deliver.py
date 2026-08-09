"""``modexctl deliver`` command — deliver content to a graph instance node.

Posts a :class:`GraphDeliverRequest` to the shared REST route
``POST /api/graphs/instances/{id}/deliver`` — the same route the WebUI
frontend uses.

Requires the graph workflow environment variables (``MODEX_WORKFLOW_ID``,
``MODEX_TASK_ID``, ``MODEX_NODE_ID``) — these are only set when the
process is spawned by a graph-scheduled ``BotAgentNode``. Regular
session agents (Pi/OpenCode subagents in non-graph contexts) do not have
these variables and cannot call deliver.

``--graph-instance-id`` defaults to ``MODEX_TASK_ID`` from the environment.
"""

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
    _missing_workflow_env_key,
    _normalize_text,
)
from bot.cli.modexctl.http_client import ControlClientError, get_control_origin
from bot.webui.routes.graph_models import GraphDeliverRequest
from modex_graph import GraphPayload


def _fetch_deliver(
    graph_instance_id: int,
    request: GraphDeliverRequest,
    workspace: str,
) -> dict[str, Any]:
    """POST GraphDeliverRequest to the shared REST deliver route.

    Uses :func:`get_control_origin` for loopback-validated base URL (same
    as ``fetch_send``). The instance ID travels in the URL path; the
    workspace travels as a ``?ws=`` query param. Raises
    :class:`ControlClientError` on connection failure or non-200 status.
    """
    origin = get_control_origin()
    url = f"{origin}/api/graphs/instances/{graph_instance_id}/deliver"
    params = {"ws": workspace} if workspace else None
    payload = request.model_dump(mode="json")

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=1.0, read=10.0, write=10.0, pool=1.0)
        ) as client:
            resp = client.post(url, json=payload, params=params)
    except httpx.RequestError as exc:
        raise ControlClientError(
            f"Failed to connect to control server at {url}: {exc}"
        ) from exc

    if resp.status_code != 200:
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error") or resp.text[:200]
        except ValueError:
            detail = resp.text[:200]
        raise ControlClientError(
            f"Control server returned HTTP {resp.status_code}: {detail}",
            status=resp.status_code,
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise ControlClientError(
            f"Control server returned non-JSON body: {exc}"
        ) from exc


def build_deliver_command(ctx: ModexCtlContext) -> Callable[..., None]:
    def _deliver(
        node_name: Annotated[
            str,
            typer.Option(
                "--node-name",
                help="Target node name within the graph instance.",
            ),
        ],
        content: Annotated[
            str,
            typer.Option(
                "--content",
                help="Content string to deliver to the node.",
            ),
        ],
        workspace: Annotated[
            str | None,
            typer.Option(
                "--workspace",
                help="Workspace path (defaults to the bot context workspace root).",
            ),
        ] = None,
        graph_instance_id: Annotated[
            int | None,
            typer.Option(
                "--graph-instance-id",
                help="Graph instance ID (defaults to MODEX_TASK_ID env var).",
            ),
        ] = None,
    ) -> None:
        """Deliver content to a graph instance node via the shared REST route."""
        missing = _missing_workflow_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        if graph_instance_id is None:
            env_task_id = os.environ.get("MODEX_TASK_ID", "")
            try:
                graph_instance_id = int(env_task_id)
            except ValueError:
                typer.echo(
                    f"error: MODEX_TASK_ID={env_task_id!r} is not a valid integer.",
                    err=True,
                )
                raise typer.Exit(code=EXIT_USAGE) from None

        effective_workspace = workspace or ""

        request = GraphDeliverRequest(
            node_name=node_name,
            content=GraphPayload(content=_normalize_text(content)),
        )

        try:
            result = _fetch_deliver(graph_instance_id, request, effective_workspace)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        message = result.get("message", f"Delivered to {node_name!r}.")
        typer.echo(message)

    return _deliver
