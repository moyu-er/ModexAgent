"""aiohttp route handlers for the sessions/messages/todos/approvals REST API.

Thin adapters extracted from :class:`bot.webui.server.WebUIServer`. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` (set by :func:`register_sessions_routes` — itself
called from :meth:`WebUIServer._setup_routes`), matching the
``control_facade`` pattern in :mod:`bot.control.routes` and the
:mod:`bot.webui.routes.models` convention.

Routes registered:
    GET    /api/sessions                                          -- list sessions.
    POST   /api/sessions                                          -- create a session.
    GET    /api/sessions/{session_id}/messages                    -- load transcript.
    GET    /api/sessions/{session_id}/todos                       -- active todos.
    GET    /api/sessions/{session_id}/approvals                   -- pending approvals.
    POST   /api/sessions/{session_id}/approvals                   -- submit decision.
    GET    /api/sessions/{session_id}/attachments/{attachment_id} -- download attachment.
    POST   /api/sessions/{session_id}/attachments                 -- upload attachment.
    GET    /api/media/config                                      -- media limits.
    DELETE /api/sessions/{session_id}                             -- delete session.

Helpers (:func:`session_store_for`, :func:`resolve_session`,
:func:`resolve_agent`, :func:`derive_sessions_from_transcripts`) are also
extracted here; :class:`WebUIServer` keeps thin delegates so the WebSocket
handlers (which stay on the server) continue to work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from aiohttp import web

from bot.adapters.channels import set_conv_channel
from bot.webui.types import (
    _DEFAULT_AGENT_NAME,
    _API_MEDIA_CONFIG_PATH,
    _API_SESSIONS_PATH,
    _API_SESSIONS_SESSION_PATH,
    _UPLOAD_CHUNK_BYTES,
    SessionListEntry,
    _entry_from_session,
    _materialize_partial_deltas,
    _new_uuid_prefix,
)
from modex_agent.core.session_id import (
    SessionInfo,
    agent_of,
    session_id_prefix_of,
)
from modex_agent.core.session_store import SessionStore
from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


# ── Helpers ─────────────────────────────────────────────────────────────────


async def session_store_for(server: WebUIServer, index_dir: Path) -> SessionStore | None:
    """Return a session store scoped to *index_dir*.

    - When a factory is injected (production), build a fresh store rooted
      at *index_dir* — one store per workspace.
    - Otherwise fall back to the single injected store (tests,
      single-workspace) and ignore *index_dir*.
    """
    if server._session_store_factory is not None:
        return await server._session_store_factory(index_dir)
    return server._session_store


async def resolve_session(
    server: WebUIServer,
    session_id: str,
    index_dir: Path | None = None,
) -> SessionInfo:
    """Resolve a SessionInfo from *session_id*.

    Prefers the session store; falls back to ``SessionInfo.from_str()``
    when no store is injected (e.g. basic tests). *index_dir* scopes the
    lookup to a workspace's session index by constructing a fresh store
    via the injected factory.
    """
    store = (
        await session_store_for(server, index_dir)
        if index_dir is not None
        else server._session_store
    )
    if store is not None:
        session = await store.get(session_id)
        if session is not None:
            return session
    return SessionInfo.from_str(session_id)


async def resolve_agent(
    server: WebUIServer,
    session_id: str,
    index_dir: Path | None = None,
) -> str:
    """Return the agent name bound to *session_id*.

    Prefers the authoritative session store; falls back to
    ``SessionInfo.from_str()`` when no store is injected.
    """
    session = await resolve_session(server, session_id, index_dir=index_dir)
    return session.agent_name


async def derive_sessions_from_transcripts(
    server: WebUIServer,
    sessions_dir: Path | None = None,
) -> list[SessionInfo]:
    """Build SessionInfo records from transcript files when the session
    index is missing or incomplete.

    Legacy workspaces only have ``.modex/sessions/<pool>/*.jsonl`` files
    and no ``.modex/session_index/``.  This fallback lets the frontend
    list and attach to those sessions without a separate migration step.
    """
    target_dir = sessions_dir if sessions_dir is not None else server._home_sessions_dir
    derived: list[SessionInfo] = []
    for session_id in await server._store.list_sessions(target_dir):
        session_prefix = session_id_prefix_of(session_id)
        if session_prefix == session_id:
            # No separator → not a usable display id.
            continue
        agent_name = agent_of(session_id)
        # Include any agent that maps to a known pool (main agents,
        # resident subagents, and dynamic subagent template types).
        pool = server._pool_for_agent_name(agent_name)
        if pool is None:
            continue
        parent_session_id: str | None = None
        # Subagent transcript (3 segments): parent is the main-agent
        # session with the same conversation prefix, if one exists.
        if session_id.count(".") == 2:
            candidates = sorted(
                sid
                for sid in await server._store.list_sessions_by_prefix(
                    session_prefix, sessions_dir=target_dir
                )
                if sid != session_id and sid.count(".") == 1
            )
            if candidates:
                parent_session_id = candidates[0]
        updated_at = await server._store.last_updated(session_id, sessions_dir=target_dir)
        created_at = updated_at
        derived.append(
            SessionInfo(
                session_id=session_id,
                agent_name=agent_name,
                parent_session_id=parent_session_id,
                created_at=created_at,
                updated_at=updated_at,
            )
        )
    return derived


async def _resolve_pool(
    server: WebUIServer,
    session: SessionInfo,
    store: SessionStore | None,
    pool_cache: dict[str, str | None],
) -> str | None:
    """Resolve the pool a session belongs to.

    Unified derivation chain (no provider-specific branches):
    1. Direct agent→pool mapping (registered agents + template types).
    2. Parent inheritance — if the agent is not registered but the
       session has a registered parent, inherit the parent's pool.
    3. None — the session is an orphan with no known pool.
    """
    pool = server._pool_for_agent_name(session.agent_name)
    if pool is not None:
        return pool
    parent_id = session.parent_session_id
    if parent_id is None:
        return None
    cached = pool_cache.get(parent_id)
    if cached is not None:
        return cached
    if parent_id in pool_cache:
        return None
    if store is not None:
        parent = await store.get(parent_id)
        if parent is not None:
            parent_pool = server._pool_for_agent_name(parent.agent_name)
            pool_cache[parent_id] = parent_pool
            return parent_pool
    pool_cache[parent_id] = None
    return None


# ── Handlers ────────────────────────────────────────────────────────────────


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


async def handle_get_messages(request: web.Request) -> web.Response:
    """``GET /api/sessions/{session_id}/messages`` -- load transcript events.

    Returns user messages (as-is) and materialized assistant turns
    (synthetic assistant_turn dicts with blocks), merged by timestamp.
    """
    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    # HTTP handlers run outside any dispatch turn, so the ctxvar is not
    # bound — resolve the sessions dir explicitly from ?ws=.
    ws_raw = request.query.get("ws", "")
    sessions_dir = server._sessions_dir_of_ws(ws_raw)
    index_dir = server._index_dir_of_ws(ws_raw)
    agent_name: str = await resolve_agent(server, session_id, index_dir=index_dir)
    pool: str = server._pool_of_agent(agent_name)
    session_prefix: str = session_id_prefix_of(session_id)

    store = server._store

    user_events: list[dict[str, object]] = [
        e.to_dict()
        for e in await store.load_sessions_by_prefix(
            session_prefix, sessions_dir=sessions_dir, pool=pool
        )
        if e.event == "user_message"
    ]

    turns = await store.load_materialized_by_prefix(
        session_prefix, sessions_dir=sessions_dir, pool=pool
    )
    assistant_events: list[dict[str, object]] = []
    for t in turns:
        assistant_events.append(
            {
                "event": "assistant_turn",
                "session_id": session_id,
                "agent_name": agent_name,
                "timestamp": t.started_at,
                "turn_id": t.turn_id,
                "blocks": t.blocks,
                "latency_ms": 0,
                # G7: SendFileToUserTool persists outbound Attachment records on
                # an AssistantTurnEvent; _materialize_events collects them onto
                # MaterializedTurn.attachments (including the standalone
                # no-turn_id carriers G7 writes) so they survive a refresh.
                "attachments": t.attachments,
            }
        )

    result = user_events + assistant_events

    # Partial streaming events — in-memory buffer, queried separately
    # from the main transcript. Attached as a synthetic streaming turn.
    load_partial = getattr(store, "load_partial", None)
    if load_partial is not None:
        partial_events = await load_partial(session_id, sessions_dir=sessions_dir)
        if partial_events:
            partial_turn = _materialize_partial_deltas(partial_events, agent_name)
            if partial_turn is not None:
                result.append(partial_turn)

    def _event_ts(event: dict[str, object]) -> int:
        ts = event.get("timestamp", 0)
        if ts is None:
            return 0
        try:
            return int(str(ts))
        except (ValueError, TypeError):
            return 0

    result.sort(key=_event_ts)
    return web.json_response(result)


async def handle_get_todos(request: web.Request) -> web.Response:
    """``GET /api/sessions/{session_id}/todos`` -- load active todos.

    Reads directly from the per-session TodoStore so the frontend can
    hydrate the todo panel when a session is reopened, even before any
    live ``todo_write``/``todo_read`` tool call arrives.

    Uses the backend-aware store from ``_store_resolver`` when wired
    (SQLite mode), falling back to ``JsonFileTodoStore`` for FILE mode.
    """
    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    ws_raw = request.query.get("ws", "")
    sessions_dir = server._sessions_dir_of_ws(ws_raw)
    index_dir = server._index_dir_of_ws(ws_raw)
    agent_name: str = await resolve_agent(server, session_id, index_dir=index_dir)
    pool: str = server._pool_of_agent(agent_name)

    store = None
    if server._store_resolver is not None:
        stores = await server._store_resolver(server._ws_root_of(ws_raw), pool)
        store = stores.todo_store
    if store is None:
        todo_dir = WorkspacePaths(root=sessions_dir.parent).runtime_dir(pool, "todos")
        store = JsonFileTodoStore(todo_dir)
    items = await store.get(session_id)
    active = [
        {"content": item.content, "status": item.status.value}
        for item in items
        if item.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
    ]
    return web.json_response(active)


async def handle_get_approvals(request: web.Request) -> web.Response:
    """``GET /api/sessions/{session_id}/approvals`` -- pending approvals (webui-only).

    Reads the persisted turn snapshots directly from the pool's turn store
    (same direct-read pattern as :func:`handle_get_todos`), so this
    works for restart/refresh recovery without a live pipeline reference.

    Uses the backend-aware store from ``_store_resolver`` when wired
    (SQLite mode), falling back to ``JsonFileTurnStateStore`` for FILE mode.
    """
    from modex_agent.agents.react.state import (
        ReActRuntimeStateCodec,
        ReActSnapshotPolicy,
    )
    from modex_agent.approval.constants import ApprovalDecision
    from modex_agent.approval.views import view_from_request
    from modex_agent.runtime.codec import RuntimeStateCodecRegistry
    from modex_agent.runtime.enums import (
        AgentKind,
        SnapshotReason,
        TurnPhase,
    )
    from modex_agent.runtime.models import StateQueryScope
    from modex_agent.runtime.store import JsonFileTurnStateStore

    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    ws_raw = request.query.get("ws", "")
    sessions_dir = server._sessions_dir_of_ws(ws_raw)
    agent_name: str = await resolve_agent(
        server, session_id, index_dir=server._index_dir_of_ws(ws_raw)
    )
    pool: str = server._pool_of_agent(agent_name)

    turn_store = None
    if server._store_resolver is not None:
        stores = await server._store_resolver(server._ws_root_of(ws_raw), pool)
        turn_store = stores.turn_store
    if turn_store is None:
        turns_dir = WorkspacePaths(root=sessions_dir.parent).runtime_dir(pool, "turns")
        codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
        turn_store = JsonFileTurnStateStore(turns_dir, codec_registry)
    # Approval turns are partitioned by workspace (turn_store path) + pool
    # + session_id, so agent_id is NOT a query dimension — matches
    # ApprovalResumer.load_pending. session_id already identifies the
    # conversation uniquely.
    snapshots = await turn_store.list_active_turns(
        StateQueryScope(
            session_id=session_id,
            phase=TurnPhase.SUSPENDED,
            reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
    )
    if not snapshots:
        return web.json_response([])
    snapshots.sort(key=lambda s: s.created_at)
    approval = ReActSnapshotPolicy.approval_from_snapshot(snapshots[-1])
    # Surface only genuinely-PENDING requests: already-decided cards must
    # not reappear after a refresh, which would force the user to re-approve.
    views = [
        view_from_request(req).to_dict()
        for req in (approval.requests if approval is not None else [])
        if approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
        == ApprovalDecision.PENDING
    ]
    return web.json_response(views)


async def handle_post_approval(request: web.Request) -> web.Response:
    """``POST /api/sessions/{session_id}/approvals`` -- submit approve/deny (webui).

    Builds an envelope carrying the structured decision and runs it through
    the webui input pipeline (reusing workspace/pool/session resolution),
    converging on the agent pipeline's approval branch.
    """
    from bot.input_pipeline.stages.resolve_pool import RoutingMeta
    from modex_agent.approval.types import ApprovalAction
    from modex_agent.approval.views import ApprovalDecisionInput
    from modex_agent.input_pipeline.envelope import UserInputEnvelope

    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    try:
        payload = await request.json()
        action = ApprovalAction(payload["action"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return web.json_response({"error": "invalid action"}, status=400)
    try:
        tool_call_id = payload["tool_call_id"]
    except KeyError:
        return web.json_response({"error": "missing tool_call_id"}, status=400)

    decision = ApprovalDecisionInput(tool_call_id=tool_call_id, action=action)
    ws_raw = request.query.get("ws", "")
    session = await resolve_session(
        server, session_id, index_dir=server._index_dir_of_ws(ws_raw)
    )
    envelope = UserInputEnvelope(
        external_id=session_id,
        content="",
        channel="websocket",
        metadata={RoutingMeta.APPROVAL_DECISION: decision},
        pre_resolved_session=session,
    )
    # Stamp the workspace (same resolver as _ws_send_message) so resume
    # reads the turn store that holds this snapshot — without it, the
    # decision silently lands on the home workspace.
    envelope.metadata[RoutingMeta.WORKSPACE] = str(server._ws_root_of(ws_raw))
    # _input_pipeline / _input_ctx are injected by WebUIService. They may
    # be None in minimal test setups -- guard so the handler degrades cleanly.
    if server._input_pipeline is None or server._input_ctx is None:
        return web.json_response({"error": "input pipeline not configured"}, status=503)
    await server._input_pipeline.handle(envelope, server._input_ctx)
    return web.json_response({"accepted": True}, status=202)


async def handle_download_attachment(request: web.Request) -> web.Response:
    """``GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=<ws>``.

    Attachment download — one endpoint, dispatch on the record's
    ``locator`` (ADR-0013 §4/§5):

    - ``media`` (inbound): resolve the byte file through the business
      :class:`WorkspaceScopedMediaStore` against the ``?ws=``-resolved media
      dir and the session's pool.
    - ``workspace`` (outbound): the file is at the literal absolute path the
      agent wrote (``att.path``).

    The ``attachment_id`` is an unguessable uuid and IS the capability — no
    auth, no signing (the WebUI is unauthenticated; ADR-0013 §5). ``?ws=``
    is routing only, resolved through the same ``_ws_root_of`` every other
    endpoint uses.

    Streaming + ``Range``/``206`` come from the HTTP layer
    (:class:`aiohttp.web.FileResponse`), not hand-rolled — outbound files
    may be up to 1 GB and must never buffer whole into memory. MIME is
    allow-listed: only ``image/*`` and ``video/*`` keep their real
    ``Content-Type``; everything else is ``application/octet-stream`` so a
    browser cannot sniff executable content. SVG responses carry a strict
    CSP. A present record whose underlying file is gone (evicted inbound,
    deleted outbound) degrades symmetrically to 404 (ADR-0013 §3/§5).
    """
    from bot.service.attachment_index import find_attachment
    from modex_agent.media.models import AttachmentLocator

    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    attachment_id: str = request.match_info["attachment_id"]
    ws_raw = request.query.get("ws", "")
    sessions_dir = server._sessions_dir_of_ws(ws_raw)

    att = await find_attachment(
        server._store, session_id, attachment_id, sessions_dir=sessions_dir
    )
    if att is None:
        return web.Response(status=404, text="attachment not found")

    path: Path | None
    if att.locator is AttachmentLocator.MEDIA:
        # Inbound: bytes are under the managed media dir. Resolve the pool
        # for the media resolver the same way the other read handlers
        # resolve it (agent_name -> pool), then read through the business
        # WorkspaceScopedMediaStore with an explicit media_dir (HTTP readers
        # run outside any dispatch turn, so the ctxvar root is unbound).
        index_dir = server._index_dir_of_ws(ws_raw)
        agent_name = await resolve_agent(server, session_id, index_dir=index_dir)
        pool = server._pool_of_agent(agent_name)
        media_store = server._input_ctx.media_store if server._input_ctx is not None else None
        if media_store is None:
            # No media resolver wired — cannot serve inbound bytes.
            return web.Response(status=404, text="attachment not found")
        media_dir = server._media_dir_of_ws(ws_raw, pool)
        path = media_store.store_for(pool, media_dir=media_dir).read(session_id, attachment_id)
    elif att.locator is AttachmentLocator.WORKSPACE:
        # Outbound: the file is at the literal absolute path the agent gave.
        path = Path(att.path)
        if not path.is_absolute():
            logger.warning(
                "Outbound attachment %s path is not absolute: %s",
                attachment_id,
                att.path,
            )
            return web.Response(status=404, text="attachment not found")
    else:  # Defensive — unknown locator value.
        logger.warning("Unknown attachment locator %r for %s", att.locator, attachment_id)
        return web.Response(status=404, text="attachment not found")

    # Symmetric 404: the Attachment record exists in the transcript, but the
    # underlying file is gone (evicted inbound / deleted outbound).
    if path is None or not path.is_file():
        return web.Response(status=404, text="attachment not found")

    # MIME allow-list: only image/* and video/* keep their real Content-Type.
    mime = att.mime or "application/octet-stream"
    serve_mime = (
        mime
        if (mime.startswith("image/") or mime.startswith("video/"))
        else "application/octet-stream"
    )

    headers: dict[str, str] = {
        "Content-Type": serve_mime,
        # nosniff: stop IE/Edge from MIME-sniffing an octet-stream body into
        # executable content (defense in depth on top of the MIME allow-list).
        "X-Content-Type-Options": "nosniff",
    }
    # SVG can carry inline script/style — pin a strict CSP so a downloaded
    # SVG opened in a browser tab cannot execute or exfiltrate.
    if serve_mime == "image/svg+xml":
        headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
        )
    # FileResponse streams the file (chunk_size) and handles HTTP Range /
    # 206 Partial Content natively, so up-to-1 GB outbound never buffers.
    return web.FileResponse(path, headers=headers)


async def handle_media_config(request: web.Request) -> web.Response:
    """``GET /api/media/config`` -- expose MediaConfig limits for pre-validation.

    Returns the active ``MediaConfig`` numbers the frontend needs to
    pre-validate a selection before uploading (ADR-0013 §7). v1 is a single
    shared config (per-pool override is a later extension; the ingest stage
    reads the same instance off the input context). When no input context is
    wired (minimal tests), the frozen ``MediaConfig()`` defaults are
    returned so the endpoint always answers with the authoritative numbers.
    """
    from modex_agent.multi_agent.pool_config.media import MediaConfig

    server: WebUIServer = request.app["server"]
    config: MediaConfig = (
        server._input_ctx.media_config if server._input_ctx is not None else MediaConfig()
    )
    return web.json_response(
        {
            "max_image_bytes": config.max_image_bytes,
            "max_text_doc_bytes": config.max_text_doc_bytes,
            "session_budget_bytes": config.session_budget_bytes,
            "max_outbound_bytes": config.max_outbound_bytes,
        }
    )


async def handle_upload_attachment(request: web.Request) -> web.Response:
    """``POST /api/sessions/{session_id}/attachments`` -- temp-file receiver.

    This endpoint is a **temp-file receiver + pre-stash**, NOT the
    authority. It saves the uploaded file under the workspace's media
    ``_tmp`` dir and returns a ref the frontend includes in the subsequent
    WS user message as an ``AttachmentRef(local_path=...)``. The actual
    perception gate + ``MediaStore.save`` + Attachment record happen in the
    ingest stage (G3) when the WS message flows through the pipeline — the
    gate stays the single authority (no duplicate gate logic here).

    A loose size pre-check rejects absurd uploads early (cap is the larger
    of the image/text-doc limits, generous on purpose); the authoritative
    per-kind cap is the pipeline's. ``?ws=`` resolves the workspace the same
    way every other handler does.
    """
    from modex_agent.multi_agent.pool_config.media import MediaConfig

    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    ws_raw = request.query.get("ws", "")

    index_dir = server._index_dir_of_ws(ws_raw)
    agent_name = await resolve_agent(server, session_id, index_dir=index_dir)
    pool = server._pool_of_agent(agent_name)

    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "file":
        return web.json_response({"error": "missing 'file' part"}, status=400)

    config = (
        server._input_ctx.media_config_for(pool) if server._input_ctx is not None else MediaConfig()
    )
    # Loose early cap: reject anything above the most generous accepted
    # limit. The authoritative per-kind gate runs in the ingest stage.
    early_cap = max(config.max_image_bytes, config.max_text_doc_bytes)

    tmp_dir = server._media_tmp_dir_of_ws(ws_raw, pool)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_name = uuid4().hex
    tmp_path = tmp_dir / tmp_name

    size = 0
    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await part.read_chunk(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > early_cap:
                    out.close()
                    tmp_path.unlink(missing_ok=True)
                    return web.json_response({"error": "file too large"}, status=413)
                out.write(chunk)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove temp upload %s", tmp_path)
        raise

    return web.json_response(
        {
            "local_path": str(tmp_path),
            "filename": part.filename or tmp_name,
            "size": size,
            "mime": part.headers.get("Content-Type"),
        }
    )


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
            "delete_session: no SessionGarbageCollector wired; skipping cascade "
            "deletion for %s",
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


# ── Registration ────────────────────────────────────────────────────────────


def register_sessions_routes(server: WebUIServer) -> None:
    """Register the sessions/messages/todos/approvals routes on ``server.app.router``.

    ``app["server"]`` is set by :func:`bot.webui.routes.models.register_models_routes`
    (called earlier from :meth:`WebUIServer._setup_routes`); this function
    relies on that slot being present so the route handlers can reach server
    state.

    Called from :meth:`WebUIServer._setup_routes`.
    """
    app = server.app
    # ``app["server"]`` is set by register_models_routes; assert defensively
    # so a future reordering surfaces a clear error rather than a KeyError
    # inside a request handler.
    if "server" not in app:
        app["server"] = server
    app.router.add_get(_API_SESSIONS_PATH, handle_sessions)
    app.router.add_post(_API_SESSIONS_PATH, handle_create_session)
    app.router.add_get(f"{_API_SESSIONS_SESSION_PATH}/messages", handle_get_messages)
    app.router.add_get(f"{_API_SESSIONS_SESSION_PATH}/todos", handle_get_todos)
    app.router.add_get(f"{_API_SESSIONS_SESSION_PATH}/approvals", handle_get_approvals)
    app.router.add_post(f"{_API_SESSIONS_SESSION_PATH}/approvals", handle_post_approval)
    app.router.add_get(
        f"{_API_SESSIONS_SESSION_PATH}/attachments/{{attachment_id}}",
        handle_download_attachment,
    )
    app.router.add_post(
        f"{_API_SESSIONS_SESSION_PATH}/attachments",
        handle_upload_attachment,
    )
    app.router.add_get(_API_MEDIA_CONFIG_PATH, handle_media_config)
    app.router.add_delete(_API_SESSIONS_SESSION_PATH, handle_delete_session)


__all__ = [
    "derive_sessions_from_transcripts",
    "handle_create_session",
    "handle_delete_session",
    "handle_download_attachment",
    "handle_get_approvals",
    "handle_get_messages",
    "handle_get_todos",
    "handle_media_config",
    "handle_post_approval",
    "handle_sessions",
    "handle_upload_attachment",
    "register_sessions_routes",
    "resolve_agent",
    "resolve_session",
    "session_store_for",
]
