"""Message + todo handlers — transcript load and active todo list.

Extracted from the original :mod:`bot.webui.routes.sessions` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared helpers in
:mod:`bot.webui.routes.sessions`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.routes.sessions import resolve_agent
from bot.webui.types import _materialize_partial_deltas
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer


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
