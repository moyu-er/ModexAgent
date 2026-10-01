"""Message + todo handlers — transcript load and active todo list.

Extracted from the original :mod:`bot.webui.routes.sessions` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared helpers in
:mod:`bot.webui.routes.sessions`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.routes.sessions import resolve_agent
from bot.webui.transcript_store import (
    MaterializedTurn,
    TranscriptRecord,
    UserMessageRecord,
    WorkspaceRoutedTranscriptStore,
    materialize_records,
)
from bot.webui.types import _DEFAULT_AGENT_NAME
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.turn.todo import TodoStatus
from modex_agent.persistence.adapters.todo_store import JsonFileTodoStore
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer


def _user_message_json(record: UserMessageRecord) -> dict[str, object]:
    """Serialize one user-message record to the history-API JSON shape.

    Byte-for-byte the dict the pre-cutover ``UserMessageEvent.to_dict()``
    served — the frontend contract.
    """
    return {
        "event": "user_message",
        "session_id": record.session_id,
        "agent_name": record.agent_name,
        "timestamp": record.timestamp_ms,
        "content": record.content,
        "attachments": record.attachments,
    }


def partial_streaming_turn(
    records: Sequence[TranscriptRecord], agent_name: str
) -> dict[str, object] | None:
    """Fold the partial streaming buffer into one synthetic streaming turn.

    The buffer holds the ``TextDelta`` / ``ThinkingDelta`` records streamed
    since the last flush boundary; the SAME framework materializer that
    replays history folds them, and this projection maps the last open
    turn onto the synthetic ``assistant_turn`` the route serves with
    ``is_streaming=True`` (the frontend renders it as the in-progress
    message and keeps appending live WS deltas on top). Returns ``None``
    when the buffer holds no foldable content.
    """
    turns = materialize_records(records)
    streaming: MaterializedTurn | None = turns[-1] if turns else None
    if streaming is None or not streaming.blocks:
        return None
    return {
        "event": "assistant_turn",
        "session_id": "",
        "agent_name": agent_name,
        "timestamp": streaming.started_at,
        "turn_id": streaming.turn_id,
        "blocks": streaming.blocks,
        "latency_ms": 0,
        "is_streaming": True,
    }


async def handle_get_messages(request: web.Request) -> web.Response:
    """``GET /api/sessions/{session_id}/messages`` -- load transcript events.

    Returns user messages (as-is) and materialized assistant turns
    (synthetic assistant_turn dicts with blocks), merged by timestamp.
    Turn folding is the framework's ``materialize_turns`` reached through
    :func:`bot.webui.transcript_store.materialize_records` — the history
    route is a pure projection of those turns onto the response JSON.
    """
    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    ws_raw = request.query.get("ws", "")
    sessions_dir = server._sessions_dir_of_ws(ws_raw)
    index_dir = server._index_dir_of_ws(ws_raw)
    agent_name: str = await resolve_agent(server, session_id, index_dir=index_dir)
    session_prefix = session_id_prefix_of(session_id)
    # Storage-partition read: explicit legacy partition fallback owned HERE
    # (infra callers apply their own fallback per the routing contract).
    pool: str = (
        server._resolve_pool_for_request(
            request.query.get("pool"), session_id_prefix_of(session_id)
        )
        or _DEFAULT_AGENT_NAME
    )

    store = server._store

    user_events: list[dict[str, object]] = [
        _user_message_json(record)
        for record in await store.load_sessions_by_prefix(
            session_prefix, sessions_dir=sessions_dir, pool=pool
        )
        if isinstance(record, UserMessageRecord)
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
                # G7: SendFileToUserTool persists outbound Attachment records
                # on a transcript carrier; materialize_records collects them
                # onto MaterializedTurn.attachments (including the standalone
                # no-turn carriers G7 writes) so they survive a refresh.
                "attachments": t.attachments,
            }
        )

    result = user_events + assistant_events

    # Partial streaming events — in-memory buffer, queried separately
    # from the main transcript, folded by the SAME framework materializer
    # and attached as a synthetic streaming turn. The capability probe is
    # the SAME store-shape check the recording tap uses
    # (isinstance against WorkspaceRoutedTranscriptStore — the only store
    # shape carrying the in-memory partial buffer).
    if isinstance(store, WorkspaceRoutedTranscriptStore):
        partial_records = await store.load_partial(session_id, sessions_dir=sessions_dir)
        if partial_records:
            partial_turn = partial_streaming_turn(partial_records, agent_name)
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
    session_id_prefix_of(session_id)
    # Storage-partition read: client param → stored route → explicit legacy
    # partition fallback (infra callers own their fallback per the routing
    # contract; the product default never guesses here).
    pool: str = server._resolve_pool_for_request(
        request.query.get("pool"), session_id_prefix_of(session_id)
    ) or _DEFAULT_AGENT_NAME

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
