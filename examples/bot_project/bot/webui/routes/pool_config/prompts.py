"""Prompt route handlers — agent prompt md CRUD.

Extracted from the original :mod:`bot.webui.routes.pool_config` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared :func:`pool_cfg_required`
guard in :mod:`bot.webui.routes.pool_config`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.config.prompt_store import PromptExistsError
from bot.service.config_controller import FieldValidationError
from bot.service.pool_config_controller import PromptInUseError
from bot.webui.routes.pool_config import pool_cfg_required

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_list_prompts(request: web.Request) -> web.Response:
    """GET /api/prompts -- list agent prompt md files (name/size/mtime)."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        prompts = server._pool_config_controller.list_prompts()
    except Exception:  # noqa: BLE001
        logger.exception("list_prompts failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([p.model_dump(mode="json") for p in prompts])


async def handle_read_prompt_strict(request: web.Request) -> web.Response:
    """GET /api/prompts/{name} -- read one prompt md WITHOUT seeding.

    Returns 404 when the file is absent (does not call ``read_or_seed``);
    returns 400 on a malformed name.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["name"]
    try:
        prompt = server._pool_config_controller.read_prompt_strict(name)
    except KeyError:
        return web.json_response({"error": f"unknown prompt: {name}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("read_prompt_strict failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response(prompt.model_dump(mode="json"))


async def handle_write_prompt_global(request: web.Request) -> web.Response:
    """PUT /api/prompts/{name} -- upsert the prompt md for ``name``.

    Reuses :meth:`PoolConfigController.write_prompt` (atomic write + marks
    ``restart_required`` on the ``prompt`` artifact class). Creates the
    file if absent (upsert semantics — no 409 on existing names).
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("write_prompt_global: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    content = body.get("content") if isinstance(body, dict) else None
    if not isinstance(content, str):
        return web.json_response(
            {"error": "validation", "fields": {"content": ["required"]}},
            status=400,
        )
    try:
        prompt = server._pool_config_controller.write_prompt(name, content)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("write_prompt_global failed")
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(prompt.model_dump(mode="json"))


async def handle_create_prompt(request: web.Request) -> web.Response:
    """POST /api/prompts -- create a new prompt md.

    Body: ``{"name": str, "content"?: str}``. Validates the name against the
    agent-name regex; rejects a duplicate name with HTTP 409 via
    :class:`PromptExistsError`. When ``content`` is omitted the seed text
    is :data:`PromptStore.DEFAULT_PROMPT_SEED`.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("create_prompt: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    name = body.get("name") if isinstance(body, dict) else None
    if not isinstance(name, str) or not name:
        return web.json_response(
            {"error": "validation", "fields": {"name": ["required"]}},
            status=400,
        )
    content = body.get("content") if isinstance(body, dict) else None
    if content is not None and not isinstance(content, str):
        return web.json_response(
            {"error": "validation", "fields": {"content": ["must be a string"]}},
            status=400,
        )
    try:
        prompt = server._pool_config_controller.create_prompt(name, content)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except PromptExistsError:
        return web.json_response(
            {"error": "exists", "name": name},
            status=409,
        )
    except Exception:  # noqa: BLE001
        logger.exception("create_prompt failed")
        return web.json_response({"error": "create failed"}, status=500)
    return web.json_response(prompt.model_dump(mode="json"), status=201)


async def handle_delete_prompt_global(request: web.Request) -> web.Response:
    """DELETE /api/prompts/{name} -- delete a prompt md if unreferenced.

    Returns 200 with ``{deleted: str}`` when unreferenced; removes the file.
    Returns 409 with ``{error: "in_use", usages: [...]}`` when any pool's
    main agent or subagent references the prompt (explicit ``prompt_name``
    match or the fallback case where ``prompt_name`` is empty and
    ``agent_name`` equals the prompt name). Returns 404 when the file does
    not exist. Does NOT remove the file on 409.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["name"]
    try:
        server._pool_config_controller.delete_prompt(name)
    except KeyError:
        return web.json_response({"error": f"unknown prompt: {name}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except PromptInUseError as exc:
        return web.json_response(
            {
                "error": "in_use",
                "usages": [u.model_dump(mode="json") for u in exc.usages],
            },
            status=409,
        )
    except Exception:  # noqa: BLE001
        logger.exception("delete_prompt failed")
        return web.json_response({"error": "delete failed"}, status=500)
    return web.json_response({"deleted": name})
