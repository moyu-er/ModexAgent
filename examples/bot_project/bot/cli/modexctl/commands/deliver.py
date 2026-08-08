"""``modexctl deliver`` command — deliver content to a graph instance node.

Posts a :class:`GraphDeliverRequest` to the shared REST route
``POST /api/graphs/instances/{id}/deliver`` — the same route the WebUI
frontend uses. Follows the ``send`` command pattern: workspace and
control origin are resolved from :class:`ModexCtlContext`, with
``--workspace`` overriding the context workspace root.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

import httpx
import typer

from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE
from bot.cli.modexctl.context import (
    ModexCtlContext,
    _echo_context_error,
    _missing_comm_env_key,
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
            detail = body.get("error", resp.text[:200])
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
        graph_instance_id: Annotated[
            int,
            typer.Option(
                "--graph-instance-id",
                help="Graph instance ID to deliver content to.",
            ),
        ],
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
    ) -> None:
        """Deliver content to a graph instance node via the shared REST route."""
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        missing_ctx = ctx.validate_history()
        if missing_ctx is not None:
            _echo_context_error(missing_ctx)
            raise typer.Exit(code=EXIT_USAGE)

        assert ctx.workspace_root is not None
        effective_workspace = workspace if workspace is not None else ctx.workspace_root

        request = GraphDeliverRequest(
            node_name=node_name,
            content=GraphPayload(content=_normalize_text(content)),
        )

        try:
            result = _fetch_deliver(graph_instance_id, request, effective_workspace)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        typer.echo(
            f"delivered to graph instance {result.get('graph_instance_id')} "
            f"node '{result.get('node_name')}': {result.get('status')}"
        )

    return _deliver
