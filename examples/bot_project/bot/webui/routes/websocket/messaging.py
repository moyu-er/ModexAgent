"""WebSocket message-send handler (SEND_MESSAGE action).

Extracted from :class:`bot.webui.server.WebUIServer` (S08). Module-level
async functions take ``server`` as first parameter (not ``self``); they
access server state directly (e.g. ``server._input_pipeline``) since they
receive the server instance as a function argument, not via ``request.app``.

Exports:
    handle_send_message(server, ws, data, state) -> None
        -- SEND_MESSAGE action: user message -> input pipeline -> enqueue,
           echoing the user message back with ``_request_id`` for the
           frontend's optimistic-message dedup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.events import DeltaEnvelope, WebUIEventType
from bot.webui.types import _DEFAULT_AGENT_NAME, _WsConnectionState, _safe_send_json

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_send_message(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    data: dict[str, object],
    state: _WsConnectionState,
) -> None:
    """SEND_MESSAGE action -- user message -> input pipeline -> enqueue.

    Builds a seed :class:`UserInputEnvelope` from the WS payload (content,
    attachments, provider/model choice, workspace, pre-resolved session),
    runs the WebUI sub-pipeline (S4..S8), and on success echoes the user
    message back to the WS client (carrying ``_request_id`` and resolved
    attachments) so the frontend can reconcile its optimistic message.

    On terminate (pipeline consumed the message -- e.g. unknown ``/skill``
    in WebUI chat) surfaces an error envelope with the pipeline's response
    message so the client sees why nothing was enqueued.
    """
    # Local imports: keep the import graph lean and mirror the original
    # in-function imports from server.py. ``_materialize_deferred_session``
    # lives in the sibling attach module (no cycle: attach imports streaming,
    # not messaging).
    from bot.webui.routes.websocket.attach import _materialize_deferred_session
    from bot.input_pipeline.stages.resolve_pool import RoutingMeta
    from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope

    session_id = str(data.get("session_id", ""))
    content = str(data.get("content", ""))
    request_id = str(data.get("_request_id", ""))
    # An attachment-only send (no text) is valid -- the frontend enables Send
    # when there are pending uploads even with empty text (ADR-0013: a file
    # in a conversation is itself the message). Drop only when there is
    # neither text nor any attachment payload.
    has_attachment_payload = (
        isinstance(data.get("attachments"), list) and len(data.get("attachments") or []) > 0
    )
    if "." not in session_id or (not content and not has_attachment_payload):
        return

    # Resolve the target workspace ("ws" == workspace) from the payload up
    # front: every per-workspace store/index call below needs it. Empty ws
    # means the home workspace. Route the bound workspace root through the
    # SAME resolver the read paths use, so a message written here is always
    # read back from the same workspace.
    ws_raw = str(data.get("ws", ""))
    index_dir = server._index_dir_of_ws(ws_raw)
    workspace_path = server._ws_root_of(ws_raw)

    # Materialize a deferred draft (created via uuid_prefix+pool attach)
    # on its first message so the session enters the index before the
    # pipeline writes the transcript. Empty drafts are never persisted.
    await _materialize_deferred_session(server, session_id, index_dir=index_dir)

    # NOTE: DO NOT call _try_intercept_control here.
    # Control slash commands (/pwd, /cd, /exit, /stop) are handled by
    # the IM pipeline (S2 EnvironmentControlStage / S3 SessionControlStage).
    # The WebUI does NOT need these -- the workspace panel and sidebar
    # controls already provide the same functionality visually.
    # In WebUI, /pwd etc. correctly reach S6 (SkillParseStage) which
    # rejects them with "builtin_not_supported". That is intentional.

    resolved = await server._resolve_session(session_id, index_dir=index_dir)
    uuid_prefix = resolved.session_id_prefix
    explicit_agent = resolved.agent_name

    # Pool resolution is OWNED by S5 (ResolvePoolStage) -- it also persists
    # the UI choice into PoolSessionStore so PoolRouter routes correctly.
    # The entry only hands the UI-selected pool (derived from the
    # session_id's agent segment) as explicit_pool; no inline resolution,
    # no _pool_switch_callback call here. (attach still uses the callback.)
    # For main agents the agent name IS the pool name; fall back to
    # explicit_agent directly when agent_pool_map lacks the entry (edge
    # case: map not yet populated during early server startup).
    explicit_pool = (
        (server._agent_pool_map.get(explicit_agent) or explicit_agent) if explicit_agent else None
    )

    # The session was already established upstream (attach / create_session).
    # Pass it through so the pipeline reuses session.session_id verbatim
    # instead of re-encoding the session_prefix (which would break
    # transcript/pool keying).  Reuse the already-resolved SessionInfo
    # from above (same args) rather than resolving a second time.
    pre_resolved = resolved

    # Run the WebUI sub-pipeline (S4..S8).
    # Build AttachmentRefs from the client payload so uploaded files (POSTed
    # to the upload endpoint, returning {local_path, filename, mime?}) are
    # NOT orphaned -- the ingest stage (G3) reads envelope.attachments and
    # would no-op on an empty list. Mirrors the QQ adapter
    # (bot/adapters/qq.py: attachments=[AttachmentRef(local_path=p) ...]).
    #
    # C1: the upload endpoint is the ONLY legitimate writer to the staging
    # dir, so an accepted local_path MUST resolve under it. A client could
    # otherwise point local_path at ANY server-readable file (e.g.
    # /etc/shadow, or a path under another workspace's data dir) and have
    # the ingest stage copy its bytes into the media store -- making them
    # agent-perceivable and downloadable (path traversal / exfiltration).
    # The QQ adapter is unaffected (it builds the ref server-side).
    raw_attachments = data.get("attachments")
    attachments: list[AttachmentRef] = []
    if isinstance(raw_attachments, list):
        # Resolve the staging pool the SAME way the upload endpoint does
        # (_pool_of_agent -> _pool_for_agent_name, incl. dynamic-subagent
        # prefix matching). ``explicit_pool`` (agent_pool_map.get or the raw
        # agent name) diverges for subagent-instance sessions and would drop
        # a legitimately-uploaded file whose temp path lives under the
        # template pool's ``_tmp`` -- the same file the upload endpoint wrote.
        staging_pool = (
            server._pool_of_agent(explicit_agent) if explicit_agent else _DEFAULT_AGENT_NAME
        )
        staging_root = server._media_tmp_dir_of_ws(ws_raw, staging_pool).resolve()
        for entry in raw_attachments:
            if not isinstance(entry, dict):
                continue
            local_path = entry.get("local_path")
            if not local_path or not isinstance(local_path, str):
                continue
            # Resolve before the containment check so symlinks / ``..``
            # segments cannot escape the staging dir.
            try:
                resolved = Path(local_path).resolve()
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Dropping WS attachment %r: path unresolvable (%s)",
                    local_path,
                    exc,
                )
                continue
            if not resolved.is_relative_to(staging_root):
                logger.warning(
                    "Dropping WS attachment %r: outside staging dir %s "
                    "(path-traversal rejection)",
                    local_path,
                    staging_root,
                )
                continue
            attachments.append(
                AttachmentRef(
                    local_path=local_path,
                    filename=entry.get("filename")
                    if isinstance(entry.get("filename"), str)
                    else None,
                    mime_type=entry.get("mime") if isinstance(entry.get("mime"), str) else None,
                )
            )

    envelope = UserInputEnvelope(
        external_id=uuid_prefix,
        content=content,
        channel="websocket",
        explicit_pool=explicit_pool,
        pre_resolved_session=pre_resolved,
        attachments=attachments,
    )
    envelope.metadata[RoutingMeta.WORKSPACE] = str(workspace_path)
    # Thread the UI-selected provider/model into the envelope so
    # ModelChoiceStage (WebUI-only) reads them off the metadata.
    provider_name = data.get("provider_name")
    model_name = data.get("model_name")
    if provider_name:
        envelope.metadata[RoutingMeta.MODEL_PROVIDER] = str(provider_name)
    if model_name:
        envelope.metadata[RoutingMeta.MODEL_MODEL] = str(model_name)
    result = await server._input_pipeline.handle(envelope, server._input_ctx)

    if result.should_continue():
        # Echo the user message back to the WS client so the frontend
        # can reconcile its optimistic message.
        final = result.envelope()
        full_sid = final.metadata[RoutingMeta.FULL_SESSION_ID]
        agent_name = final.metadata[RoutingMeta.RESOLVED_AGENT]
        pool_name = final.metadata[RoutingMeta.RESOLVED_POOL]
        from bot.webui.events import UserMessageEvent

        event = UserMessageEvent(
            session_id=full_sid,
            agent_name=agent_name,
            content=content,
            # Mirror persist_user_message.py:43 -- carry the resolved
            # Attachment records so the sender's own attachments render on
            # their optimistic message mid-session, not only after a
            # transcript reload. resolved_attachments may be None/empty for
            # legacy messages; guard with ``or []``.
            attachments=[a.to_dict() for a in (final.resolved_attachments or [])],
        )
        meta: dict[str, object] = {}
        if request_id:
            meta["_request_id"] = request_id
        await _safe_send_json(
            ws, DeltaEnvelope.from_event(event, meta, pool=pool_name).to_dict()
        )
    else:
        # Terminate: pipeline consumed the message (e.g. /cd /pwd /exit
        # in WebUI chat which has no S2/S3, or unknown /skill).
        # Surface the reason to the client as an error envelope.
        response = result.response
        message = ""
        if response is not None:
            try:
                message = str(response["message"])
            except (KeyError, TypeError):
                pass
        pool = explicit_pool or _DEFAULT_AGENT_NAME
        await _safe_send_json(
            ws,
            DeltaEnvelope(
                session_id=session_id,
                agent_name=explicit_agent or _DEFAULT_AGENT_NAME,
                event_type=WebUIEventType.ERROR.value,
                pool=pool,
                payload={"message": message or "unsupported command in WebUI chat"},
            ).to_dict(),
        )


__all__ = [
    "handle_send_message",
]
