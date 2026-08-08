"""WebSocket graph event subscription handlers (SUBSCRIBE_GRAPH / UNSUBSCRIBE_GRAPH).

Extracted per the one-action-per-submodule convention of
:mod:`bot.webui.routes.websocket`. Module-level async functions take
``server`` as first parameter (not ``self``); they access server state
directly since they receive the server instance as a function argument, not
via ``request.app``.

The subscription protocol follows PRD §11.2 (graph-visualization-redesign):

- Client sends ``{action: "subscribe_graph", instance_id, ws?}``. ``ws`` is
  the workspace identifier, resolved through
  ``server._graph_workspace_resolver`` exactly like the graph REST routes
  (:mod:`bot.webui.routes.graph_routes`).
- On success the server registers a fresh ``asyncio.Queue`` in the
  workspace's ``graph_event_subscribers[instance_id]`` list and starts a
  :func:`forward_graph_events` drain loop (queue consumption ==
  ``ws.send_json``), then acks with ``graph_subscribed``.
- Events pushed to subscribers have the standalone shape::

      {"type": "graph_event",
       "graph_instance_id": "<str>",
       "event": <GraphOutput.model_dump(mode="json")>}

  ``graph_instance_id`` is serialized as ``str`` (snowflake ids exceed
  JS ``Number.MAX_SAFE_INTEGER``), matching the graph_models.py convention.
  The shape deliberately does NOT use ``DeltaEnvelope`` (that envelope
  requires a session id and carries chat-stream semantics).
- ``unsubscribe_graph`` / disconnect cancel the forward task and remove the
  queue from the registry (``_WsConnectionState.cleanup_graph_subscriptions``).

The producer side is NOT here: events originate only from
``WebUIGraphOutputAdapter.emit()`` (the single emission seam), which writes
the event store AND fans out to the subscriber queues.

Exports:
    handle_subscribe_graph(server, ws, data, state) -> None
        -- SUBSCRIBE_GRAPH action: queue registration + forward loop startup.
    handle_unsubscribe_graph(server, ws, data, state) -> None
        -- UNSUBSCRIBE_GRAPH action: task cancel + queue deregistration.
    forward_graph_events(instance_id, queue, ws) -> None
        -- background task: drain a subscription queue to the WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.types import _GraphSubscription, _safe_send_json, _WsConnectionState
from modex_graph import GraphOutput

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")

GRAPH_EVENT_MESSAGE_TYPE = "graph_event"
_GRAPH_SUBSCRIBED_MESSAGE_TYPE = "graph_subscribed"
_GRAPH_UNSUBSCRIBED_MESSAGE_TYPE = "graph_unsubscribed"
_GRAPH_ERROR_MESSAGE_TYPE = "graph_error"


def _parse_instance_id(data: dict[str, object]) -> int | None:
    """Parse the ``instance_id`` message field (int or decimal str)."""
    raw = data.get("instance_id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return None
    return None


async def forward_graph_events(
    instance_id: int,
    queue: asyncio.Queue[GraphOutput],
    ws: web.WebSocketResponse,
) -> None:
    """Background task: drain a graph subscription queue to the WebSocket.

    Mirrors :func:`bot.webui.routes.websocket.streaming.forward_deltas`:
    queue consumption is a ``ws.send_json`` of the standalone graph_event
    shape; cancellation / a broken connection ends the loop quietly.
    """
    try:
        while True:
            output = await queue.get()
            await ws.send_json(
                {
                    "type": GRAPH_EVENT_MESSAGE_TYPE,
                    "graph_instance_id": str(instance_id),
                    "event": output.model_dump(mode="json"),
                }
            )
    except (asyncio.CancelledError, ConnectionError):
        pass
    except Exception:
        logger.exception("Graph event forwarding error for instance %s", instance_id)


async def handle_subscribe_graph(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    data: dict[str, object],
    state: _WsConnectionState,
) -> None:
    """SUBSCRIBE_GRAPH action -- register a queue + start the forward loop.

    The workspace's ``graph_event_subscribers`` registry is the same dict the
    ``WebUIGraphOutputAdapter`` fans out to, so subscribing here is the only
    wiring needed for events to start flowing. Subscribing twice to the same
    instance is idempotent (the first subscription is kept).
    """
    instance_id = _parse_instance_id(data)
    if instance_id is None:
        await _safe_send_json(
            ws,
            {
                "type": _GRAPH_ERROR_MESSAGE_TYPE,
                "message": f"invalid instance_id: {data.get('instance_id')!r}",
            },
        )
        return
    if instance_id in state.graph_subscriptions:
        return  # already subscribed on this connection

    resolver = server._graph_workspace_resolver
    ws_id = str(data.get("ws", ""))
    resources = resolver(ws_id) if resolver is not None else None
    if resources is None or resources.graph_event_subscribers is None:
        await _safe_send_json(
            ws,
            {
                "type": _GRAPH_ERROR_MESSAGE_TYPE,
                "message": "graph workspace not configured",
            },
        )
        return

    registry = resources.graph_event_subscribers
    queue: asyncio.Queue[GraphOutput] = asyncio.Queue()
    registry.setdefault(instance_id, []).append(queue)
    task = asyncio.create_task(forward_graph_events(instance_id, queue, ws))
    state.graph_subscriptions[instance_id] = _GraphSubscription(
        instance_id=instance_id,
        registry=registry,
        queue=queue,
        task=task,
    )
    await _safe_send_json(
        ws,
        {
            "type": _GRAPH_SUBSCRIBED_MESSAGE_TYPE,
            "graph_instance_id": str(instance_id),
        },
    )


async def handle_unsubscribe_graph(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    data: dict[str, object],
    state: _WsConnectionState,
) -> None:
    """UNSUBSCRIBE_GRAPH action -- cancel the forward task + drop the queue.

    Unsubscribing an instance this connection never subscribed to is a no-op
    (still acked), so client reconnect/resubscribe races stay harmless.
    """
    instance_id = _parse_instance_id(data)
    if instance_id is None:
        await _safe_send_json(
            ws,
            {
                "type": _GRAPH_ERROR_MESSAGE_TYPE,
                "message": f"invalid instance_id: {data.get('instance_id')!r}",
            },
        )
        return
    subscription = state.graph_subscriptions.pop(instance_id, None)
    if subscription is not None:
        queues = subscription.registry.get(instance_id)
        if queues is not None and subscription.queue in queues:
            queues.remove(subscription.queue)
        if not subscription.task.done():
            subscription.task.cancel()
            try:
                await subscription.task
            except asyncio.CancelledError:
                pass
    await _safe_send_json(
        ws,
        {
            "type": _GRAPH_UNSUBSCRIBED_MESSAGE_TYPE,
            "graph_instance_id": str(instance_id),
        },
    )


__all__ = [
    "GRAPH_EVENT_MESSAGE_TYPE",
    "forward_graph_events",
    "handle_subscribe_graph",
    "handle_unsubscribe_graph",
]
