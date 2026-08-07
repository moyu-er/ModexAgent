"""MCP registry route handlers — read / upsert / delete.

Extracted from the original :mod:`bot.webui.routes.pool_config` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared :func:`pool_cfg_required`
guard in :mod:`bot.webui.routes.pool_config`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.service.config_controller import FieldValidationError
from bot.webui.routes.pool_config import pool_cfg_required

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_read_mcp(request: web.Request) -> web.Response:
    """GET /api/mcp -- read the typed MCP registry mapping."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        registry = server._pool_config_controller.read_mcp()
    except Exception:  # noqa: BLE001
        logger.exception("read_mcp failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response(
        {name: e.model_dump(mode="json", by_alias=True) for name, e in registry.items()}
    )


async def handle_upsert_mcp(request: web.Request) -> web.Response:
    """POST/PUT /api/mcp/{server} -- insert or update one MCP server entry."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["server"]
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert_mcp: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    try:
        entry = server._pool_config_controller.upsert_mcp(name, body)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("upsert_mcp failed")
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(entry.model_dump(mode="json", by_alias=True))


async def handle_delete_mcp(request: web.Request) -> web.Response:
    """DELETE /api/mcp/{server} -- remove one MCP server (refuses if referenced)."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["server"]
    try:
        server._pool_config_controller.delete_mcp(name)
    except KeyError:
        return web.json_response({"error": f"unknown server: {name}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("delete_mcp failed")
        return web.json_response({"error": "delete failed"}, status=500)
    return web.json_response({"deleted": name})
