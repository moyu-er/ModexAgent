"""Pool listing route (ticket 11 — read-only).

Pool trees are edited through the scope declaration editor
(``PUT /api/scope/declaration``, ticket 16); the legacy pool.yml CRUD
routes (create/read/write/delete pool + bidirectional peers) retired with
the legacy roster road. This module keeps the one read endpoint the chat
composer's pool selector consumes, backed by the declaration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.routes.pool_config import pool_cfg_required

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_list_pools(request: web.Request) -> web.Response:
    """GET /api/pools -- list declared pool summaries."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        pools = server._pool_config_controller.list_pools()
    except Exception:  # noqa: BLE001
        logger.exception("list_pools failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([p.model_dump(mode="json") for p in pools])
