"""Delta forwarding and queue-watching for WebSocket connections.

Extracted from :class:`bot.webui.server.WebUIServer` (S09). Module-level
async functions take ``server`` as first parameter (not ``self``); they
access server state directly (e.g. ``server._input``) since they receive
the server instance as a function argument, not via ``request.app``.

Exports:
    forward_deltas(server, session_id, ws) -> None
        -- background task: drain a session's delta queue to the WebSocket.
    watch_new_queues(server, ws, state) -> None
        -- poll for dynamically-created subagent queues and start forwarding.

Internal helper:
    _queue_belongs_to_connection(attached_sessions, session_id) -> bool
        -- ws-partitioning convergence guard.
"""

from __future__ import annotations

import asyncio
import logging
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
    server: WebUIServer, session_id: str, ws: web.WebSocketResponse
) -> None:
    """Background task: read DeltaEnvelopes and send as structured JSON."""
    try:
        q = server._input.get_delta_queue(session_id)
        if q is None:
            return
        while True:
            envelope: DeltaEnvelope = await q.get()
            await ws.send_json(envelope.to_dict())
    except (asyncio.CancelledError, ConnectionError):
        pass
    except Exception:
        logger.exception("Delta forwarding error for session %s", session_id)


def _queue_belongs_to_connection(attached_sessions: list[str], session_id: str) -> bool:
    """True if *session_id*'s conversation is already owned by this connection.

    Convergence point for ws isolation on the shared WebSocket adapter: the
    adapter multiplexes every workspace/tab through one set of delta queues,
    keyed only by session id. A dynamically-created subagent queue
    (``{conv}.{agent}.{inv}``) belongs to whichever connection attached that
    conversation. We derive that from the connection's own
    ``attached_sessions`` -- every attached session shares one conversation
    prefix -- so no per-connection ws bookkeeping is needed: claim a queue
    only when its prefix matches a conversation this connection already owns.
    """
    prefix = session_id_prefix_of(session_id)
    return any(session_id_prefix_of(s) == prefix for s in attached_sessions)


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
                if not _queue_belongs_to_connection(state.attached_sessions, session_id):
                    # Belongs to another connection's conversation; let that
                    # connection's own watcher claim it.
                    continue
                state.attached_sessions.append(session_id)
                state.forward_tasks.append(
                    asyncio.create_task(forward_deltas(server, session_id, ws))
                )
    except (asyncio.CancelledError, ConnectionError):
        pass
    except Exception:
        logger.exception("Queue watcher error")


__all__ = [
    "forward_deltas",
    "watch_new_queues",
]
