"""Approval handlers — pending approval list + submit decision.

Extracted from the original :mod:`bot.webui.routes.sessions` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared helpers in
:mod:`bot.webui.routes.sessions`.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.routes.sessions import resolve_session
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer


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
    session_prefix = session_id_prefix_of(session_id)
    pool: str = server._resolve_pool_for_request(request.query.get("pool"), session_prefix)

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
    from modex_agent.input_pipeline.envelope import UserInputEnvelope
    from modex_agent.messaging.models import ApprovalAction, ApprovalDecisionInput

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
    pool = request.query.get("pool") or ""
    session = await resolve_session(server, session_id, index_dir=server._index_dir_of_ws(ws_raw))
    envelope = UserInputEnvelope(
        external_id=session_id,
        content="",
        channel="websocket",
        metadata={RoutingMeta.APPROVAL_DECISION: decision},
        pre_resolved_session=session,
    )
    if pool:
        envelope.metadata[RoutingMeta.RESOLVED_POOL] = pool
    envelope.metadata[RoutingMeta.WORKSPACE] = str(server._ws_root_of(ws_raw))
    # _input_pipeline / _input_ctx are injected by WebUIService. They may
    # be None in minimal test setups -- guard so the handler degrades cleanly.
    if server._input_pipeline is None or server._input_ctx is None:
        return web.json_response({"error": "input pipeline not configured"}, status=503)
    await server._input_pipeline.handle(envelope, server._input_ctx)
    return web.json_response({"accepted": True}, status=202)
