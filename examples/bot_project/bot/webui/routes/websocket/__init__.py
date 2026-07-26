"""WebSocket route handlers extracted from :class:`bot.webui.server.WebUIServer`.

This package owns the GET /ws endpoint and the action dispatch for WebSocket
text messages. Sub-modules own the per-action handlers:

- :mod:`bot.webui.routes.websocket.attach` -- ATTACH action.
- :mod:`bot.webui.routes.websocket.messaging` -- SEND_MESSAGE action.
- :mod:`bot.webui.routes.websocket.control` -- PAUSE / DELETE_CONVERSATION actions.
- :mod:`bot.webui.routes.websocket.streaming` -- delta forwarding + queue watcher.

Module-level async functions take ``server`` as first parameter (not ``self``);
they access server state directly (e.g. ``server._input``) since they receive
the server instance as a function argument, not via ``request.app``.

Exports:
    register_websocket_routes(server) -> None
        -- register the GET /ws route on ``server.app.router``.
    dispatch_ws_message(server, ws, raw, state) -> None
        -- parse and dispatch a single WebSocket text message to the
           matching per-action handler in the sub-modules.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.events import WebSocketAction
from bot.webui.types import _WS_PATH, _WsConnectionState

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def _handle_websocket(
    server: WebUIServer, request: web.Request
) -> web.WebSocketResponse:
    """WebSocket endpoint -- handles attach, send_message, new/delete session.

    Creates a fresh :class:`_WsConnectionState`, iterates incoming WS text
    messages (dispatching each through :func:`dispatch_ws_message`), and on
    disconnect runs the state's cleanup (drain queues, cancel forward tasks,
    unregister sessions) so a closed tab never leaks stale deltas to a
    later re-attach.
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    state = _WsConnectionState()

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                await dispatch_ws_message(server, ws, msg.data, state)
            elif msg.type == web.WSMsgType.ERROR:
                logger.error("WebSocket error: %s", ws.exception())
    except Exception:
        logger.exception("WebSocket handler error")
    finally:
        await state.cleanup(server._input)

    return ws


async def dispatch_ws_message(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    raw: str,
    state: _WsConnectionState,
) -> None:
    """Parse and dispatch a single WebSocket text message.

    Routes the parsed ``action`` to the matching per-action handler in the
    sub-modules (imported lazily inside each branch so this package's import
    graph stays acyclic and so a handler module that fails to import does
    not break the others).
    """
    try:
        data: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in WebSocket message")
        return

    action = str(data.get("action", ""))

    if action == WebSocketAction.ATTACH:
        from bot.webui.routes.websocket.attach import handle_attach

        await handle_attach(server, ws, data, state)
    elif action == WebSocketAction.SEND_MESSAGE:
        from bot.webui.routes.websocket.messaging import handle_send_message

        await handle_send_message(server, ws, data, state)
    elif action == WebSocketAction.PAUSE:
        from bot.webui.routes.websocket.control import handle_pause

        await handle_pause(server, ws, data)
    elif action == WebSocketAction.DELETE_CONVERSATION:
        from bot.webui.routes.websocket.control import handle_delete_conversation

        await handle_delete_conversation(server, ws, data)
    else:
        logger.warning("Unknown WebSocket action: %s", action)


def register_websocket_routes(server: WebUIServer) -> None:
    """Register the GET /ws WebSocket route on ``server.app.router``.

    The aiohttp route handler closes over *server* so the WebSocket handlers
    receive the server instance as a function argument (matching the
    :mod:`bot.webui.routes.websocket` package convention) instead of going
    through ``request.app["server"]`` -- the WS handlers need direct access
    to many server internals (``_input``, ``_input_pipeline``, ``_store``,
    ``_pool_*`` helpers, ...) and the closure avoids the indirection.

    Called from :meth:`WebUIServer._setup_routes`.
    """

    async def _ws_route_handler(request: web.Request) -> web.WebSocketResponse:
        return await _handle_websocket(server, request)

    server.app.router.add_get(_WS_PATH, _ws_route_handler)


__all__ = [
    "dispatch_ws_message",
    "register_websocket_routes",
]
