"""WebSocket session attach handler (ATTACH action).

Extracted from :class:`bot.webui.server.WebUIServer` (S07). Module-level
async functions take ``server`` as first parameter (not ``self``); they
access server state directly (e.g. ``server._input``) since they receive
the server instance as a function argument, not via ``request.app``.

Exports:
    handle_attach(server, ws, data, state) -> None
        -- ATTACH action: session registration, pool switching, deferred
           materialize, pool-agent / subagent queue registration, watcher
           startup, and the final ``attached`` envelope.

Internal helper:
    _materialize_deferred_session(server, session_id, index_dir) -> None
        -- persist a deferred (uuid_prefix-prefixed) session on first message.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.events import DeltaEnvelope, WebUIEventType
from bot.webui.types import _DEFAULT_AGENT_NAME, _safe_send_json, _WsConnectionState
from modex_agent.core.session_id import (
    SessionInfo,
    agent_of,
    session_id_prefix_of,
)

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def _materialize_deferred_session(
    server: WebUIServer, session_id: str, index_dir: Path | None = None
) -> None:
    """Persist a deferred (uuid_prefix-prefixed) session on first message.

    Attach creates a provisional id ``{uuid_prefix}.{agent}`` without
    persisting; this materializes it just before the pipeline writes the
    transcript, using ``create_with_prefix`` so ``uuid_prefix`` is
    the verbatim session_prefix -- same id, no re-encoding.  Already-persisted
    sessions (reattach, existing conversations) are a no-op. *index_dir*
    scopes the record to the message's workspace session index.
    """
    if server._session_factory is None:
        return
    store = (
        await server._session_store_for(index_dir)
        if index_dir is not None
        else server._session_store
    )
    if store is None:
        return
    if await store.get(session_id) is not None:
        return  # already persisted
    session_prefix = session_id_prefix_of(session_id)
    agent = agent_of(session_id, default="unknown")
    session = server._session_factory.create_with_prefix(
        agent_name=agent,
        prefix=session_prefix,
    )
    if session.session_id != session_id:
        # Fallback: session_prefix contained a separator or was empty; persist a
        # from_str record so the session list still shows the conversation.
        session = SessionInfo.from_str(session_id)
    await store.save(session)


async def handle_attach(
    server: WebUIServer,
    ws: web.WebSocketResponse,
    data: dict[str, object],
    state: _WsConnectionState,
) -> None:
    """ATTACH action -- session registration, pool switching, deferred materialize.

    The WebSocket client attaches to a conversation by sending either:

    - ``{uuid_prefix, pool, ws}`` for a NEW conversation -- the session id is
      built as ``{uuid_prefix}.{agent}`` (deferred, not persisted until the
      first message), or
    - ``{session_id, ws}`` for an EXISTING conversation -- the session is
      resolved from the per-workspace session index.

    On attach the connection registers the main session, all pool-agent
    sessions, all subagent sessions found in the transcript, and all
    children known to the relation store, then starts a ``forward_deltas``
    task per registered session plus a ``watch_new_queues`` watcher for
    dynamically-created subagent queues. Pool routing is persisted through
    the configured switch callback (with a failsafe direct write to the
    pool session store when the callback is not wired).
    """
    # Local import: streaming imports nothing from this module, so no cycle.
    from bot.webui.routes.websocket.streaming import forward_deltas, watch_new_queues

    session_id = str(data.get("session_id", ""))

    # The workspace ("ws") the client attached under -- scopes every
    # transcript / session-index read in this attach so history and
    # subagent discovery never cross workspace boundaries. Empty == home.
    attach_ws_raw = str(data.get("ws", ""))
    attach_sessions_dir = server._sessions_dir_of_ws(attach_ws_raw)
    attach_index_dir = server._index_dir_of_ws(attach_ws_raw)

    # ── New conversation path: frontend sends uuid_prefix + pool ──
    uuid_prefix_raw = str(data.get("uuid_prefix", ""))
    pool_from_client = str(data.get("pool", ""))

    if uuid_prefix_raw and pool_from_client:
        agent_name = (
            server._agent_resolver(pool_from_client)
            if server._agent_resolver is not None
            else pool_from_client
        )
        if server._pool_agent_names and agent_name not in server._pool_agent_names:
            await _safe_send_json(
                ws,
                DeltaEnvelope(
                    session_id=session_id or "",
                    agent_name=agent_name,
                    event_type=WebUIEventType.ERROR.value,
                    pool=pool_from_client,
                    payload={"message": f"unknown pool: {pool_from_client}"},
                ).to_dict(),
            )
            return
        # Deferred creation: empty drafts are NOT persisted -- the client's
        # uuid_prefix is used verbatim as the session_prefix so the session id
        # (``{uuid_prefix}.{agent}``) stays stable through attach->send.
        # Persistence happens on the first message (handle_send_message).
        session_id = f"{uuid_prefix_raw}.{agent_name}"
        session_prefix = uuid_prefix_raw
        uuid_prefix = uuid_prefix_raw

        # Defensive: if a transcript already exists for this session_id
        # (reattach of a persisted session that already received a message),
        # routing is already established -- attach is idempotent.
        try:
            if await server._store.load(session_id, sessions_dir=attach_sessions_dir):
                pass  # Session persisted; attach is idempotent, routing intact.
        except Exception as exc:
            logger.warning("Failed to check existing transcript for %s: %s", session_id, exc)
    else:
        if not session_id or "." not in session_id:
            await _safe_send_json(
                ws,
                DeltaEnvelope(
                    session_id=session_id or "",
                    agent_name=_DEFAULT_AGENT_NAME,
                    event_type=WebUIEventType.ERROR.value,
                    payload={"message": "session_id required"},
                ).to_dict(),
            )
            return
        resolved = await server._resolve_session(session_id, index_dir=attach_index_dir)
        session_prefix = resolved.session_id_prefix
        uuid_prefix = session_prefix

    # Unregister any previous sessions and cancel their forward tasks.
    # cleanup() sets state._stopped (to halt the previous watcher); reset
    # it here because this state is being reused for a fresh attach cycle
    # and the new watcher spawned below must run. Graph subscriptions are
    # orthogonal to the attached conversation -- switching sessions must
    # NOT clear them, so they are excluded from this cleanup.
    await state.cleanup(server._input, include_graphs=False)
    state._stopped = False

    server._input.register_connection(session_id, ws)
    state.attached_sessions.append(session_id)

    # PoolRouter's session store is the single source of truth for routing.
    # pool_from_client is the user's explicit choice from the UI dropdown;
    # use it directly as the pool name without going through agent_pool_map
    # (which may not yet be populated in every edge case).
    pool_name = server._resolve_pool_for_request(pool_from_client or None, uuid_prefix)
    if server._pool_switch_callback is not None:
        await asyncio.to_thread(server._pool_switch_callback, session_prefix, pool_name)
    # Failsafe: if the callback is not wired (edge case during early
    # startup or test setups), write directly through the input context's
    # pool_session_store so the PoolRouter can still read the mapping.
    elif server._input_ctx is not None and server._input_ctx.pool_session_store is not None:
        server._input_ctx.pool_session_store.set(session_prefix, pool_name)

    # Proactively register ALL pool agent sessions so deltas from any
    # pool's agent are forwarded to this WebSocket client.
    # Use the already-resolved session_prefix (encoded for new conversations,
    # the persisted session_prefix for existing sessions) so the derived ids
    # match the transcript/delta-queue keys -- do NOT re-encode.
    for agent_name in server._pool_agent_names:
        if agent_name == _DEFAULT_AGENT_NAME:
            continue  # already registered above
        pool_sid = f"{session_prefix}.{agent_name}"
        if server._input.get_delta_queue(pool_sid) is None:
            server._input.register_connection(pool_sid, ws)
            state.attached_sessions.append(pool_sid)
            state.forward_tasks.append(asyncio.create_task(forward_deltas(server, pool_sid, ws)))

    # Also register subagent sessions found in transcript (for history).
    # These are full session ids (``{conv}.{agent}.{invocation_id}``); each
    # invocation is a distinct session.  ``session_prefix`` is the stable
    # conversation prefix used by the transcript store.
    for sub_sid in sorted(
        await server._store.list_sessions_by_prefix(
            session_prefix, sessions_dir=attach_sessions_dir
        )
    ):
        sub_agent_name = agent_of(sub_sid, default="unknown")
        # Main-agent sessions have exactly two segments ({prefix}.{agent})
        # and were already registered in the pool_agent_names loop above.
        # Subagent invocations have three segments ({prefix}.{agent}.{inv})
        # and must always be registered -- even when the invocation_id
        # coincidentally matches a pool agent name, which would confuse
        # ``SessionInfo.from_str``'s rightmost-segment parsing.
        is_main_agent_session = (
            sub_sid.count(".") == 1 and sub_agent_name in server._pool_agent_names
        )
        if is_main_agent_session:
            continue
        if server._input.get_delta_queue(sub_sid) is None:
            server._input.register_connection(sub_sid, ws)
            state.attached_sessions.append(sub_sid)
            state.forward_tasks.append(asyncio.create_task(forward_deltas(server, sub_sid, ws)))

    # Also register subagent sessions from relation store -- these may have
    # been dispatched but not yet written to transcript.
    attach_store = await server._session_store_for(attach_index_dir)
    if attach_store is not None:
        for parent_sid in list(state.attached_sessions):
            for child_session in await attach_store.get_children(parent_sid):
                child_sid = str(child_session)
                if server._input.get_delta_queue(child_sid) is None:
                    server._input.register_connection(child_sid, ws)
                    state.attached_sessions.append(child_sid)
                    state.forward_tasks.append(
                        asyncio.create_task(forward_deltas(server, child_sid, ws))
                    )

    # Watch for dynamically-created subagent delta queues (created by
    # send_envelope auto-create).  When a new queue appears for a
    # session_id not yet forwarded, start a forward_deltas task.
    state.forward_tasks.append(asyncio.create_task(watch_new_queues(server, ws, state)))

    state.forward_tasks.append(asyncio.create_task(forward_deltas(server, session_id, ws)))

    att_agent = await server._resolve_agent(session_id, index_dir=attach_index_dir)
    att_pool = server._resolve_pool_for_request(pool_name or None, uuid_prefix)
    await _safe_send_json(
        ws,
        DeltaEnvelope(
            session_id=session_id,
            agent_name=att_agent,
            event_type=WebUIEventType.ATTACHED.value,
            pool=att_pool,
        ).to_dict(),
    )


__all__ = [
    "handle_attach",
]
