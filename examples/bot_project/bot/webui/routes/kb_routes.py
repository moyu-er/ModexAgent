from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, assert_never

from aiohttp import web
from pydantic import ValidationError

from bot.kb.formatting import (
    format_delete_confirmation,
    format_entry,
    format_key_list,
    format_search_results,
    format_upsert_confirmation,
)
from bot.kb.models import (
    KbAction,
    KbControlRequest,
    KbUpsertRequest,
)

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

logger = logging.getLogger(__name__)


async def handle_kb_post(request: web.Request) -> web.Response:
    workspace = request.query.get("workspace")
    if not workspace:
        return web.json_response({"error": "workspace is required"}, status=400)

    try:
        body = KbControlRequest.model_validate(await request.json())
    except (json.JSONDecodeError, ValidationError, web.HTTPException):
        return web.json_response({"error": "invalid request"}, status=400)

    server: WebUIServer = request.app["server"]
    try:  # noqa: BLE001 - HTTP boundary converts unexpected failures to generic 500s
        resolver = server._graph_workspace_resolver
        resources = resolver(workspace) if resolver is not None else None
        provider = resources.kb_provider if resources is not None else None
        if provider is None:
            return web.json_response(
                {"error": "knowledge service unavailable"}, status=500
            )

        match body.action:
            case KbAction.SEARCH:
                if body.query_or_key is None:
                    return web.json_response({"error": "query is required"}, status=400)
                results = await provider.search(
                    body.query_or_key, body.filter, body.limit
                )
                result = format_search_results(results)
            case KbAction.GET:
                if body.query_or_key is None:
                    return web.json_response({"error": "key is required"}, status=400)
                entry = await provider.get(body.query_or_key, body.filter)
                result = format_entry(entry, body.query_or_key)
            case KbAction.SET:
                if body.query_or_key is None or body.value is None:
                    return web.json_response(
                        {"error": "key and value are required"}, status=400
                    )
                entry = await provider.upsert(
                    KbUpsertRequest(
                        key=body.query_or_key,
                        value=body.value,
                        task_id=body.filter.task_id or "",
                        session_id=body.filter.session_id or "",
                        category=body.filter.category or "",
                    )
                )
                result = format_upsert_confirmation(entry)
            case KbAction.DELETE:
                if body.query_or_key is None:
                    return web.json_response({"error": "key is required"}, status=400)
                deleted = await provider.delete(body.query_or_key, body.filter)
                result = format_delete_confirmation(deleted, body.query_or_key)
            case KbAction.LIST:
                keys = await provider.list_keys(body.filter, body.query_or_key)
                result = format_key_list(keys)
            case unreachable:
                assert_never(unreachable)
    except Exception:  # noqa: BLE001  # noqa: BROAD_EXCEPT_OK
        logger.exception("KB request failed")
        return web.json_response({"error": "knowledge request failed"}, status=500)

    return web.json_response({"result": result})


def register_kb_routes(server: WebUIServer) -> None:
    if "server" not in server.app:
        server.app["server"] = server
    server.app.router.add_post("/api/control/kb", handle_kb_post)


__all__ = ["register_kb_routes"]
