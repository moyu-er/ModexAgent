"""aiohttp route handlers for the sessions/messages/todos/approvals REST API.

Thin adapters extracted from :class:`bot.webui.server.WebUIServer`. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` (set by :func:`register_sessions_routes` — itself
called from :meth:`WebUIServer._setup_routes`), matching the
``control_facade`` pattern in :mod:`bot.control.routes` and the
:mod:`bot.webui.routes.models` convention.

This package owns the sessions/messages/todos/approvals/attachments REST
endpoints. Sub-modules own the per-concern handlers:

- :mod:`bot.webui.routes.sessions.lifecycle` -- create / list / delete.
- :mod:`bot.webui.routes.sessions.messages` -- transcript + todos.
- :mod:`bot.webui.routes.sessions.approvals` -- pending approvals + submit.
- :mod:`bot.webui.routes.sessions.attachments` -- upload / download / config.

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

Shared helpers (:func:`session_store_for`, :func:`resolve_session`,
:func:`resolve_agent`, :func:`derive_sessions_from_transcripts`,
:func:`_resolve_pool`) live in this module so every handler sub-module can
import them; :class:`WebUIServer` keeps thin delegates so the WebSocket
handlers (which stay on the server) continue to work.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bot.webui.types import (
    _API_MEDIA_CONFIG_PATH,
    _API_SESSIONS_PATH,
    _API_SESSIONS_SESSION_PATH,
)
from modex_agent.core.session_id import (
    SessionInfo,
    agent_of,
    session_id_prefix_of,
)
from modex_agent.core.session_store import SessionStore

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

    Pool is resolved via ``_pool_resolver`` (PoolSessionStore — the
    authoritative session_prefix→pool mapping).  No agent_name
    reverse-engineering.
    """
    target_dir = sessions_dir if sessions_dir is not None else server._home_sessions_dir
    derived: list[SessionInfo] = []
    for session_id in await server._store.list_sessions(target_dir):
        session_prefix = session_id_prefix_of(session_id)
        if session_prefix == session_id:
            # No separator → not a usable display id.
            continue
        agent_name = agent_of(session_id)
        # Resolve pool via the authoritative PoolSessionStore lookup.
        pool = server._resolve_pool_by_prefix(session_prefix)
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
    """Resolve the pool a session belongs to via PoolSessionStore.

    Pool is looked up by session_prefix (conversation id) — the
    authoritative persisted mapping written by S5 ResolvePoolStage.
    No agent_name reverse-engineering.
    """
    prefix = session.session_id_prefix
    if prefix in pool_cache:
        return pool_cache[prefix]
    pool = server._resolve_pool_by_prefix(prefix)
    pool_cache[prefix] = pool
    return pool


# ── Handler sub-modules ─────────────────────────────────────────────────────
# Imported AFTER the helpers above are defined so the sub-modules can
# ``from bot.webui.routes.sessions import session_store_for, ...`` without
# hitting a partially-initialised package (circular-import guard).
# noqa: E402 — justified by the ordering constraint above.
from bot.webui.routes.sessions.approvals import (  # noqa: E402
    handle_get_approvals,
    handle_post_approval,
)
from bot.webui.routes.sessions.attachments import (  # noqa: E402
    handle_download_attachment,
    handle_media_config,
    handle_upload_attachment,
)
from bot.webui.routes.sessions.lifecycle import (  # noqa: E402
    handle_create_session,
    handle_delete_session,
    handle_sessions,
)
from bot.webui.routes.sessions.messages import (  # noqa: E402
    handle_get_messages,
    handle_get_todos,
)

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
