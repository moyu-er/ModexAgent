"""Delta forwarding and queue-watching for WebSocket connections.

Extracted from :class:`bot.webui.server.WebUIServer` (S09). Module-level
async functions take ``server`` as first parameter (not ``self``); they
access server state directly (e.g. ``server._input``) since they receive
the server instance as a function argument, not via ``request.app``.

Exports:
    forward_deltas(session_id, ws, queue) -> None
        -- background task: drain one connection's delta queue to its WebSocket.
    watch_new_queues(server, ws, state) -> None
        -- poll for dynamically-created subagent queues and start forwarding.

Internal helper:
    _queue_belongs_to_connection(attached_sessions, session_id) -> bool
        -- ws-partitioning convergence guard.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.events import DeltaEnvelope
from modex_agent.core.session_id import session_id_prefix_of

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer
    from bot.webui.types import _WsConnectionState

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def forward_deltas(
    session_id: str, ws: web.WebSocketResponse, queue: asyncio.Queue[DeltaEnvelope]
) -> None:
    """Background task: drain THIS connection's delta queue to the WebSocket.

    The queue is the one returned by ``register_connection`` — with multicast
    queues each connection owns its own, so another tab attaching the same
    session never steals this drainer's stream.
    """
    try:
        while True:
            envelope: DeltaEnvelope = await queue.get()
            await ws.send_json(envelope.to_dict())
    except (asyncio.CancelledError, ConnectionError):
        pass
    except Exception:
        logger.exception("Delta forwarding error for session %s", session_id)


def _queue_belongs_to_connection(
    attached_sessions: list[str],
    session_id: str,
    ancestors: Iterable[str] = (),
) -> bool:
    """True if *session_id*'s conversation is already owned by this connection.

    Two matching strategies:
    1. Prefix match — sessions sharing the same conversation prefix.
    2. Ancestor match — subagent sessions created by
       ``SubagentDispatchStrategy`` use ``invocation_id`` as prefix, so
       prefix matching alone misses them. The adapter's genealogy map
       records each dynamically-created child → parent link; walk the
       chain (cycle-guarded in ``WebSocketInputAdapter.ancestors``) until
       an ancestor already in ``attached_sessions`` is found. Handles
       arbitrary nesting depth (subagent-of-subagent-of-subagent).
    """
    prefix = session_id_prefix_of(session_id)
    if any(session_id_prefix_of(s) == prefix for s in attached_sessions):
        return True
    attached_set = set(attached_sessions)
    return any(ancestor in attached_set for ancestor in ancestors)


async def watch_new_queues(
    server: WebUIServer, ws: web.WebSocketResponse, state: _WsConnectionState
) -> None:
    """Periodically check for dynamically-created delta queues and start
    forwarding tasks for any that are not yet being drained.

    Subagent sessions dispatched after the initial attach have their delta
    queues auto-created by ``send_envelope``, but no ``forward_deltas``
    task is running for them.  This watcher discovers those queues and
    starts forwarding.

    ws-scoped: only queues whose conversation this connection already owns
    are claimed (see :func:`_queue_belongs_to_connection`), so a subagent
    stream from one workspace/tab is never bound to another connection.
    """
    try:
        while True:
            await asyncio.sleep(1.0)
            if state._stopped:
                # cleanup() has started: stop claiming queues so we never
                # append a session / spawn a task that cleanup just cleared.
                break
            for session_id in list(server._input._delta_queues):
                if state._stopped:
                    break
                if session_id in state.attached_sessions:
                    continue
                if not _queue_belongs_to_connection(
                    state.attached_sessions,
                    session_id,
                    server._input.ancestors(session_id),
                ):
                    # Belongs to another connection's conversation; let that
                    # connection's own watcher claim it.
                    continue
                state.attached_sessions.append(session_id)
                q = server._input.register_connection(session_id, ws)
                state.forward_tasks.append(
                    asyncio.create_task(forward_deltas(session_id, ws, q))
                )
    except (asyncio.CancelledError, ConnectionError):
        pass
    except Exception:
        logger.exception("Queue watcher error")


__all__ = [
    "forward_deltas",
    "watch_new_queues",
]
