"""WebSocket pause / delete-conversation control handlers.

Extracted from :class:`bot.webui.server.WebUIServer` (S09). Module-level
async functions take ``server`` as first parameter (not ``self``); they
access server state directly (e.g. ``server._input``) since they receive
the server instance as a function argument, not via ``request.app``.

Exports:
    handle_pause(server, ws, data) -> None
        -- PAUSE action: cancel the running turn for the selected session.
    handle_delete_conversation(server, ws, data) -> None
        -- DELETE_CONVERSATION action: cascade-delete a session tree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.events import DeltaEnvelope, WebUIEventType
from bot.webui.types import _safe_send_json

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_pause(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    data: dict[str, object],
) -> None:
    """Cancel the running turn for the selected session.

    The WebSocket input adapter is configured with the shared control filter,
    so reusing _try_intercept_control("/stop", ...) sends a CANCEL_TURN
    command through InMemoryControlChannel. The interceptors in the active
    pool drain the command and abort the turn.

    When the control command is not handled (filter not configured, or an
    unexpected parse failure), an error envelope is surfaced to the client
    so the pause button never silently does nothing.
    """
    session_id = str(data.get("session_id", ""))
    if "." not in session_id:
        return

    ws_raw = str(data.get("ws", ""))
    pool_from_payload = str(data.get("pool", ""))
    index_dir = server._index_dir_of_ws(ws_raw)
    resolved = await server._resolve_session(session_id, index_dir=index_dir)
    handled = await server._input._try_intercept_control("/stop", resolved.session_id)
    if not handled:
        session_prefix = resolved.session_id_prefix
        pool = server._resolve_pool_for_request(pool_from_payload or None, session_prefix)
        await _safe_send_json(
            ws,
            DeltaEnvelope(
                session_id=resolved.session_id,
                agent_name=resolved.agent_name,
                event_type=WebUIEventType.ERROR.value,
                pool=pool,
                payload={"message": "No turn to pause -- the agent is currently idle."},
            ).to_dict(),
        )


async def handle_delete_conversation(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    data: dict[str, object],
) -> None:
    """Delete a conversation's full cascade (delegates to the collector).

    Mirrors the REST delete handler: resolves ws root + pool and delegates to
    the SessionGarbageCollector, which removes the root's record synchronously
    and drains the cascade + ten artifact types via the background pool.
    Supersedes the old prefix-based delete (subagents carry their own prefix,
    so prefix-delete missed the cascade -- ADR-0018).
    """
    session_id = str(data.get("session_id", ""))
    if "." not in session_id:
        return
    ws_raw = str(data.get("ws", ""))
    pool_from_payload = str(data.get("pool", ""))
    index_dir = server._index_dir_of_ws(ws_raw)
    resolved = await server._resolve_session(session_id, index_dir=index_dir)
    agent_name = resolved.agent_name
    session_prefix = resolved.session_id_prefix
    pool = server._resolve_pool_for_request(pool_from_payload or None, session_prefix)
    if server._session_gc is not None:
        await server._session_gc.delete_session_tree(
            session_id, ws_root=server._ws_root_of(ws_raw), pool=pool
        )
    else:
        logger.warning(
            "delete_conversation: no SessionGarbageCollector wired; skipping "
            "cascade deletion for %s",
            session_id,
        )
    await _safe_send_json(
        ws,
        DeltaEnvelope(
            session_id=session_id,
            agent_name=agent_name,
            event_type=WebUIEventType.CONVERSATION_DELETED.value,
            pool=pool,
        ).to_dict(),
    )


__all__ = [
    "handle_delete_conversation",
    "handle_pause",
]
