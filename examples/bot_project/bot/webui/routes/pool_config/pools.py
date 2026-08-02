"""Pool + peer route handlers — CRUD and bidirectional peer edges.

Extracted from the original :mod:`bot.webui.routes.pool_config` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared :func:`pool_cfg_required`
guard in :mod:`bot.webui.routes.pool_config`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web
from pydantic import ValidationError

from bot.service.config_controller import FieldValidationError
from bot.service.pool_config_controller import PoolNotEmptyError
from bot.webui.routes.pool_config import pool_cfg_required

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_list_pools(request: web.Request) -> web.Response:
    """GET /api/pools -- list pool summaries."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        pools = server._pool_config_controller.list_pools()
    except Exception:  # noqa: BLE001
        logger.exception("list_pools failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([p.model_dump(mode="json") for p in pools])


async def handle_create_pool(request: web.Request) -> web.Response:
    """POST /api/pools -- create a pool. Body: {"name": "<pool>"}."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_pool: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name:
        return web.json_response(
            {"error": "validation", "fields": {"name": ["required"]}},
            status=400,
        )
    try:
        tree = server._pool_config_controller.create_pool(name)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("create_pool failed")
        return web.json_response({"error": "create failed"}, status=500)
    return web.json_response(tree.model_dump(mode="json"))


async def handle_read_pool(request: web.Request) -> web.Response:
    """GET /api/pools/{pool} -- read one pool tree."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    try:
        tree = server._pool_config_controller.read_pool(pool)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("read_pool failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response(tree.model_dump(mode="json"))


async def handle_write_pool(request: web.Request) -> web.Response:
    """PUT /api/pools/{pool} -- validate + persist a pool tree. Body = PoolTree."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("write_pool: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    try:
        from modex_agent.multi_agent.pool_config import PoolSpec

        tree = PoolSpec.model_validate(body)
    except Exception as exc:  # noqa: BLE001 - pydantic validation
        from bot.service.config_controller import _flatten_errors

        if isinstance(exc, ValidationError):
            return web.json_response(
                {"error": "validation", "fields": _flatten_errors(exc)},
                status=400,
            )
        return web.json_response(
            {"error": "validation", "fields": {"body": [str(exc)]}},
            status=400,
        )
    try:
        written = server._pool_config_controller.write_pool(pool, tree)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("write_pool failed")
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(written.model_dump(mode="json"))


async def handle_delete_pool(request: web.Request) -> web.Response:
    """DELETE /api/pools/{pool} -- delete a pool."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    try:
        server._pool_config_controller.delete_pool(pool)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except PoolNotEmptyError as exc:
        return web.json_response(
            {"error": "pool_not_empty", "busy_agents": exc.busy_agents},
            status=409,
        )
    except Exception:  # noqa: BLE001
        logger.exception("delete_pool failed")
        return web.json_response({"error": "delete failed"}, status=500)
    return web.json_response({"deleted": pool})


async def handle_add_peer(request: web.Request) -> web.Response:
    """POST /api/pools/{pool}/peers -- add a bidirectional peer edge.

    Body: {"peer": "<other_pool>"}. On success both sides of the edge
    are written and both updated pool trees are returned so the UI can
    refresh the current pool and any visible peer pool.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("add_peer: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    peer = body.get("peer") if isinstance(body, dict) else None
    if not isinstance(peer, str) or not peer:
        return web.json_response(
            {"error": "validation", "fields": {"peer": ["required"]}},
            status=400,
        )
    try:
        tree_a, tree_b = server._pool_config_controller.add_peer(pool, peer)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("add_peer failed")
        return web.json_response({"error": "add peer failed"}, status=500)
    return web.json_response(
        {
            "pool_a": tree_a.model_dump(mode="json"),
            "pool_b": tree_b.model_dump(mode="json"),
        }
    )


async def handle_remove_peer(request: web.Request) -> web.Response:
    """DELETE /api/pools/{pool}/peers/{peer} -- remove a bidirectional peer edge.

    Both sides of the edge are removed atomically. Returns both updated
    pool trees.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    peer = request.match_info["peer"]
    try:
        tree_a, tree_b = server._pool_config_controller.remove_peer(pool, peer)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("remove_peer failed")
        return web.json_response({"error": "remove peer failed"}, status=500)
    return web.json_response(
        {
            "pool_a": tree_a.model_dump(mode="json"),
            "pool_b": tree_b.model_dump(mode="json"),
        }
    )
