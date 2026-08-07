"""Session lifecycle handlers — create / list / delete.

Extracted from the original :mod:`bot.webui.routes.sessions` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared helpers in
:mod:`bot.webui.routes.sessions`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from aiohttp import web

from bot.adapters.channels import set_conv_channel
from bot.webui.routes.sessions import (
    _resolve_pool,
    derive_sessions_from_transcripts,
    resolve_session,
    session_store_for,
)
from bot.webui.types import (
    _DEFAULT_AGENT_NAME,
    SessionListEntry,
    _entry_from_session,
    _new_uuid_prefix,
)

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_create_session(request: web.Request) -> web.Response:
    """``POST /api/sessions`` -- create a new session.

    Optional JSON body: ``{"pool": "pool_name", "ws": "<workspace path>"}``.
    ``ws`` scopes the new session to a workspace's session index (home when
    absent) so it never leaks into another workspace's listing.
    """
    server: WebUIServer = request.app["server"]
    pool_name: str | None = None
    ws_raw: str = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw_pool = body.get("pool")
            if isinstance(raw_pool, str) and raw_pool:
                pool_name = raw_pool
            raw_ws = body.get("ws")
            if isinstance(raw_ws, str):
                ws_raw = raw_ws
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse /api/sessions JSON body: %s", exc)
    index_dir = server._index_dir_of_ws(ws_raw)

    effective_pool = pool_name or _DEFAULT_AGENT_NAME
    agent_name = (
        server._agent_resolver(effective_pool)
        if server._agent_resolver is not None
        else effective_pool
    )
    if server._session_factory is not None:
        session = server._session_factory.create(agent_name)
        session_id = session.session_id
        session_prefix = session.session_id_prefix
        created_at = session.created_at
        updated_at = session.updated_at
        store = await session_store_for(server, index_dir)
        if store is not None:
            await store.save(session)
    else:
        uuid_prefix = _new_uuid_prefix()
        session_id = f"{uuid_prefix}.{agent_name}"
        session_prefix = uuid_prefix
        created_at = None
        updated_at = None
    set_conv_channel(session_prefix, "websocket")
    if server._pool_switch_callback is not None:
        await asyncio.to_thread(server._pool_switch_callback, session_prefix, effective_pool)
    return web.json_response(
        {
            "session_id": session_id,
            "agent_name": agent_name,
            "pool": effective_pool,
            "parent_session_id": None,
            "created_at": created_at,
            "updated_at": updated_at,
        }
    )


async def handle_sessions(request: web.Request) -> web.Response:
    """``GET /api/sessions`` -- list sessions visible in the current workspace.

    Query ``?pool=X`` to filter to a single pool (default: all pools).
    Query ``?ws=<path>`` to filter to a specific workspace directory.
    All sessions are listed; the frontend builds the tree from
    ``parent_session_id`` — root nodes have ``parent_session_id: null``.

    Sessions are hard-partitioned by workspace: the listing reads ONLY this
    workspace's session index + transcript dir. Home (no ``?ws=``) lists
    only home's sessions — it never leaks other workspaces' sessions.

    Falls back to deriving SessionInfo records from transcript files when
    the session index is empty or incomplete, so legacy workspaces (which
    only have ``.modex/sessions/``) still render existing conversations.
    """
    server: WebUIServer = request.app["server"]
    pool_filter: str | None = request.query.get("pool")
    ws_raw = request.query.get("ws", "")
    index_dir = server._index_dir_of_ws(ws_raw)
    sessions_dir = server._sessions_dir_of_ws(ws_raw)
    session_list: list[SessionListEntry] = []
    seen_session_ids: set[str] = set()

    store = await session_store_for(server, index_dir)
    pool_cache: dict[str, str | None] = {}
    if store is not None:
        for session in await store.list_sessions():
            session_id = session.session_id
            if session_id in seen_session_ids:
                continue
            pool = await _resolve_pool(server, session, store, pool_cache)
            if pool is None:
                continue
            if pool_filter and pool != pool_filter:
                continue
            seen_session_ids.add(session_id)
            pool_cache[session_id] = pool
            session_list.append(_entry_from_session(session, pool))

    for session in await derive_sessions_from_transcripts(server, sessions_dir):
        session_id = session.session_id
        if session_id in seen_session_ids:
            continue
        pool = await _resolve_pool(server, session, store, pool_cache)
        if pool is None:
            continue
        if pool_filter and pool != pool_filter:
            continue
        seen_session_ids.add(session_id)
        pool_cache[session_id] = pool
        session_list.append(_entry_from_session(session, pool))

    session_list.sort(key=lambda s: s.updated_at or 0, reverse=True)
    return web.json_response([asdict(entry) for entry in session_list])


async def handle_delete_session(request: web.Request) -> web.Response:
    """``DELETE /api/sessions/{session_id}`` -- delete a conversation's cascade.

    Delegates to the SessionGarbageCollector: resolves the workspace root +
    pool, then the collector sync-removes the root's record + transcript
    (conversation leaves the list immediately) and drains the full subagent
    cascade + all ten artifact types via the background pool. Keeps the
    {deleted: id} contract. If no collector is wired (should not happen in
    production), logs a warning and returns without deleting.
    """
    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    if server._session_gc is None:
        # No collector wired (should not happen in production — web_ui_service
        # wires one unconditionally at start). Surface it loudly rather than
        # silently re-running the old shallow delete (which would skip the
        # cascade + artifacts this feature exists to clean).
        logger.warning(
            "delete_session: no SessionGarbageCollector wired; skipping cascade deletion for %s",
            session_id,
        )
        return web.json_response({"deleted": session_id})
    ws_raw = request.query.get("ws", "")
    ws_root = server._ws_root_of(ws_raw)
    index_dir = server._index_dir_of_ws(ws_raw)
    sessions_dir = server._sessions_dir_of_ws(ws_raw)
    resolved = await resolve_session(server, session_id, index_dir=index_dir)
    pool = server._pool_of_agent(resolved.agent_name)
    await server._session_gc.delete_session_tree(session_id, ws_root=ws_root, pool=pool)
    clear_partial = getattr(server._store, "clear_partial", None)
    if clear_partial is not None:
        try:
            await clear_partial(session_id, sessions_dir=sessions_dir)
        except Exception as exc:
            logger.warning("clear_partial failed during delete for %s: %s", session_id, exc)
    return web.json_response({"deleted": session_id})
