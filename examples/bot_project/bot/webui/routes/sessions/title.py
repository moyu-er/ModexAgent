"""Session title route — PATCH /api/sessions/{session_id}/title.

Thin HTTP adapter over :class:`bot.service.session_title.SessionTitleOps`
(the single title owner). The ops instance is the workspace's real one,
resolved through the assembly's existing workspace materialization owner, so manual renames
share the runtime registry with pool turns and the GC cleanup path.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.service.session_title import (
    TitleValidationError,
    normalize_title,
)
from bot.webui.types import _API_SESSIONS_SESSION_PATH

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server.
logger = logging.getLogger("bot.webui.server")

_API_SESSION_TITLE_PATH = f"{_API_SESSIONS_SESSION_PATH}/title"


async def handle_patch_session_title(request: web.Request) -> web.Response:
    """``PATCH /api/sessions/{session_id}/title?ws=&pool=`` — rename a session.

    Body: ``{"title": "<new title>"}``. Trimmed, must be non-empty,
    single-line, at most 80 chars → ``400`` on violation; unknown session
    (or workspace without resources) → ``404``; success returns
    ``{"session_id", "title"}`` and fires the ``sessions_changed``
    notification.
    """
    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    ws_raw = request.query.get("ws", "")

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict) or "title" not in body:
        return web.json_response({"error": "missing 'title' field"}, status=400)
    raw_title = body["title"]
    if not isinstance(raw_title, str):
        return web.json_response({"error": "'title' must be a string"}, status=400)
    try:
        normalize_title(raw_title)
    except TitleValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    # Only a mutation needs the running registry. Ordinary FILE index/GC
    # reads remain lightweight and must not start pools in cold workspaces.
    resources = await server._ensure_workspace_resources(ws_raw)
    ops = resources.title_ops if resources is not None else None
    if ops is None:
        return web.json_response(
            {"error": "workspace resources not available"}, status=404
        )
    requested_pool = request.query.get("pool")
    if requested_pool:
        owner_pool = await server._resolve_session_pool(session_id, ws_raw)
        if owner_pool is not None and owner_pool != requested_pool:
            return web.json_response({"error": "session is not in the requested pool"}, status=404)
    try:
        title = await ops.set_title(session_id, raw_title)
    except LookupError:
        return web.json_response(
            {"error": f"session {session_id!r} not found"}, status=404
        )
    except TitleValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    # PA-02: lightweight list-invalidation control notification (carries
    # the canonical workspace path). Failure is non-fatal — the frontend
    # re-reads the authoritative list on next open/refresh.
    return web.json_response({"session_id": session_id, "title": title})


__all__ = ["handle_patch_session_title"]
