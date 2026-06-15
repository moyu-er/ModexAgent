"""aiohttp WebUI server with REST API and WebSocket support.

Workspace handling:
  - Transcripts live in ONE shared flat store, the same store the agent
    emitter and IM FanIn write to.  The framework is NOT workspace-aware.
  - Workspace is a pure backend-service concern.  Session→workspace attribution
    is owned by :class:`bot.service.workspace_store.WorkspaceScopedTranscriptStore`,
    which the WebUI consumes through the :class:`WorkspaceIndex` interface defined
    here (dependency direction stays service → webui).  When no index is
    injected (e.g. basic tests), all main-agent sessions are listed unfiltered.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import pathvalidate
from aiohttp import web

from bot.adapters.channels import set_conv_channel
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.webui.events import (
    DeltaEnvelope,
    UserMessageEvent,
    WebSocketAction,
    WebUIEventType,
)
from framework.core.session_id import SessionInfo, SessionIdFactory, agent_of, snowflake_of
from framework.core.session_store import SessionStore
from bot.webui.transcript_store import TranscriptStore

logger = logging.getLogger(__name__)


@dataclass
class _WsConnectionState:
    """Tracks all sessions and forward tasks bound to one WebSocket connection."""

    attached_sessions: list[str] = field(default_factory=list)
    forward_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def cleanup(self, input_adapter: WebSocketInputAdapter) -> None:
        """Cancel all forward tasks and unregister all sessions."""
        for task in self.forward_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.forward_tasks.clear()
        for session_id in self.attached_sessions:
            input_adapter.unregister_connection(session_id)
        self.attached_sessions.clear()


# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_AGENT_NAME: str = "main"
_API_SESSIONS_PATH: str = "/api/sessions"
_API_SESSIONS_SESSION_PATH: str = "/api/sessions/{session_id}"
_API_POOLS_PATH: str = "/api/pools"
_WS_PATH: str = "/ws"
_WEBUI_STATIC_PREFIX: str = "/webui/"
_DEFAULT_STATIC_DIST: Path = Path(__file__).resolve().parent.parent / "web" / "dist"


def _new_uuid_prefix() -> str:
    """Generate a new 12-char uuid prefix for a session_id."""
    return uuid4().hex[:12]


# ── Workspace membership seam (owned by the consumer) ──────────────────────


class WorkspaceIndex(ABC):
    """Workspace- and pool-partitioned transcript access the WebUI needs.

    Implemented by the business layer
    (:class:`bot.service.workspace_store.WorkspaceScopedTranscriptStore`).
    Defined here so the WebUI does not depend on ``bot.service`` (dependency
    direction stays service → webui).
    """

    @abstractmethod
    def store_for(self, workspace: str, pool: str) -> TranscriptStore:
        """Return the physical transcript store for *workspace* + *pool*."""
        ...

    @abstractmethod
    def pools_in(self, workspace: str) -> list[str]:
        """Return pool names that exist under *workspace*."""
        ...


# ── Server ─────────────────────────────────────────────────────────────────


class WebUIServer:
    """HTTP + WebSocket server for the bot WebUI.

    Transcripts are stored in ONE shared flat store (the same store the agent
    pipeline writes to).  Workspace is a pure backend-service concept: the
    server filters/listens to session→workspace attribution through an injected
    :class:`WorkspaceIndex`.  The framework (emitter / agents) is never
    workspace-aware.
    """

    def __init__(
        self,
        input_adapter: WebSocketInputAdapter,
        transcript_store: TranscriptStore,
        static_dist: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._input: WebSocketInputAdapter = input_adapter
        # Shared flat transcript store -- same store the agent emitter and IM
        # FanIn write to.  All transcript I/O (read + write) goes through it.
        self._store: TranscriptStore = transcript_store
        self._static_dist: Path | None = static_dist
        self._data_dir: Path | None = data_dir

        self._delta_tasks: dict[str, asyncio.Task[None]] = {}

        # Session store (WorkspacePoolSessionStore) -- injected by WebUIService.
        self._session_store: SessionStore | None = None
        # SessionIdFactory -- injected by WebUIService for creating new sessions.
        self._session_factory: SessionIdFactory | None = None

        # Workspace context -- injected by WebUIService for the workspace API.
        self._workspace_ctx = None
        # Workspace+pool partition index -- injected by WebUIService.  When None,
        # the flat shared store is used (workspace-agnostic, basic tests).
        self._workspace_index: WorkspaceIndex | None = None
        # Pool metadata -- injected by WebUIService after pool initialization.
        self._pool_agent_names: list[str] = [_DEFAULT_AGENT_NAME]
        self._pool_switch_callback: Callable[[str, str], None] | None = None
        self._pool_resolver: Callable[[str], str | None] | None = None
        self._agent_resolver: Callable[[str], str] | None = None
        self._agent_pool_map: dict[str, str] = {}
        self._recent_workspaces = None  # set by WebUIService
        self._input_pipeline = None  # injected by WebUIService
        self._input_ctx = None

        self.app = web.Application()
        self._setup_routes()

    # ------------------------------------------------------------------
    # Workspace helpers
    # ------------------------------------------------------------------

    def _current_workspace(self) -> str:
        """Return the current workspace path (empty string if none configured)."""
        if self._workspace_ctx is None:
            return ""
        return str(self._workspace_ctx.current)

    def _pool_of_agent(self, agent_name: str) -> str:
        """Return the pool an agent belongs to (default main)."""
        return self._pool_for_agent_name(agent_name) or _DEFAULT_AGENT_NAME

    def _pool_for_agent_name(self, agent_name: str) -> str | None:
        """Return the pool for *agent_name*, including dynamic subagent instances.

        The agent→pool map contains main agents and template types.  Dynamic
        subagent instances have names like ``reviewer-abc123``; they inherit
        the pool of their template type.
        """
        if agent_name in self._agent_pool_map:
            return self._agent_pool_map[agent_name]
        for template_type, pool in self._agent_pool_map.items():
            if agent_name.startswith(f"{template_type}-"):
                return pool
        return None

    def _active_store(self, pool: str) -> TranscriptStore:
        """Return the physical store for the current workspace + *pool*.

        When no workspace index is injected (basic tests), fall back to the
        flat shared store.
        """
        if self._workspace_index is None:
            return self._store
        return self._workspace_index.store_for(self._current_workspace(), pool)

    # ------------------------------------------------------------------
    # Late-binding configuration (called by WebUIService after init)
    # ------------------------------------------------------------------

    def set_pool_agent_names(self, names: list[str]) -> None:
        """Set the list of pool agent names for proactive delta registration."""
        self._pool_agent_names = list(names)

    def set_pool_switch_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for setting pool routing: callback(snowflake, pool_name)."""
        self._pool_switch_callback = callback

    def set_pool_resolver(self, callback: Callable[[str], str | None]) -> None:
        """Set callback for reading current pool: callback(conv_id) -> pool_name."""
        self._pool_resolver = callback

    def set_agent_resolver(self, callback: Callable[[str], str]) -> None:
        """Set callback for resolving pool_name -> main_agent_name."""
        self._agent_resolver = callback

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set mapping from main_agent_name -> pool_name for session list labels."""
        self._agent_pool_map = dict(mapping)

    def set_workspace_context(self, ctx) -> None:
        """Inject the WorkspaceContext for workspace API."""
        self._workspace_ctx = ctx

    def set_workspace_index(self, index: WorkspaceIndex) -> None:
        """Inject the session→workspace membership index."""
        self._workspace_index = index

    def set_session_store(self, store: SessionStore) -> None:
        """Inject the session store for SessionInfo-based operations."""
        self._session_store = store

    def set_session_factory(self, factory: SessionIdFactory) -> None:
        """Inject the SessionIdFactory for creating new sessions."""
        self._session_factory = factory

    def set_recent_workspaces(self, recent) -> None:
        """Inject the RecentWorkspaces store for the recent-workspaces API."""
        self._recent_workspaces = recent
        recent.load()

    def set_input_pipeline(self, pipeline) -> None:
        """Inject the WebUI user-input pipeline."""
        self._input_pipeline = pipeline

    def set_input_context(self, ctx) -> None:
        """Inject the shared input-pipeline context."""
        self._input_ctx = ctx

    # ------------------------------------------------------------------
    # SessionInfo resolution helpers
    # ------------------------------------------------------------------

    async def _resolve_session(self, session_id: str) -> SessionInfo:
        """Resolve a SessionInfo from *session_id*.

        Prefers the session store; falls back to ``SessionInfo.from_str()``
        when no store is injected (e.g. basic tests).
        """
        if self._session_store is not None:
            session = await self._session_store.get(session_id)
            if session is not None:
                return session
        return SessionInfo.from_str(session_id)

    async def _resolve_agent(self, session_id: str) -> str:
        """Return the agent name bound to *session_id*.

        Prefers the authoritative session store; falls back to
        ``SessionInfo.from_str()`` when no store is injected.
        """
        session = await self._resolve_session(session_id)
        return session.agent_name

    def _derive_sessions_from_transcripts(self) -> list[SessionInfo]:
        """Build SessionInfo records from transcript files when the session
        index is missing or incomplete.

        Legacy workspaces only have ``.modex/sessions/<pool>/*.jsonl`` files
        and no ``.modex/session_index/``.  This fallback lets the frontend
        list and attach to those sessions without a separate migration step.
        """
        derived: list[SessionInfo] = []
        for session_id in self._store.list_sessions():
            snowflake = snowflake_of(session_id)
            if snowflake == session_id:
                # No separator → not a usable display id.
                continue
            agent_name = agent_of(session_id)
            # Include any agent that maps to a known pool (main agents,
            # resident subagents, and dynamic subagent template types).
            pool = self._pool_for_agent_name(agent_name)
            if pool is None:
                continue
            parent_session_id: str | None = None
            # Subagent transcript (3 segments): parent is the main-agent
            # session with the same conversation prefix, if one exists.
            if session_id.count(".") == 2:
                candidates = sorted(
                    sid
                    for sid in self._store.list_sessions_in_conversation(snowflake)
                    if sid != session_id and sid.count(".") == 1
                )
                if candidates:
                    parent_session_id = candidates[0]
            updated_at = self._store.last_updated(session_id)
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

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        """Register REST and WebSocket routes on the aiohttp Application."""
        self.app.router.add_get(_API_POOLS_PATH, self._handle_pools)
        self.app.router.add_get("/api/workspace", self._handle_workspace)
        self.app.router.add_get("/api/workspace/browse", self._handle_workspace_browse)
        self.app.router.add_post("/api/workspace/cd", self._handle_workspace_cd)
        self.app.router.add_get("/api/workspace/recent", self._handle_workspace_recent)
        self.app.router.add_get(_API_SESSIONS_PATH, self._handle_sessions)
        self.app.router.add_post(_API_SESSIONS_PATH, self._handle_create_session)
        self.app.router.add_get(
            f"{_API_SESSIONS_SESSION_PATH}/messages", self._handle_get_messages
        )
        self.app.router.add_delete(_API_SESSIONS_SESSION_PATH, self._handle_delete_session)
        self.app.router.add_get(_WS_PATH, self._handle_websocket)

        if self._static_dist is not None:
            self.app.router.add_get(
                _WEBUI_STATIC_PREFIX,
                self._handle_static_index,
            )
            self.app.router.add_static(
                _WEBUI_STATIC_PREFIX,
                path=str(self._static_dist),
                show_index=False,
            )
        else:
            self.app.router.add_get(_WEBUI_STATIC_PREFIX, self._handle_no_static)
            self.app.router.add_get(f"{_WEBUI_STATIC_PREFIX}{{tail:.*}}", self._handle_no_static)

    # ------------------------------------------------------------------
    # REST handlers
    # ------------------------------------------------------------------

    async def _handle_pools(self, request: web.Request) -> web.Response:
        """GET /api/pools -- list available pool names."""
        pools: list[dict[str, str]] = [
            {"name": name} for name in sorted(self._pool_agent_names)
        ]
        return web.json_response(pools)

    async def _handle_workspace(self, request: web.Request) -> web.Response:
        """GET /api/workspace -- return current workspace path and home status."""
        cwd: str = ""
        home: str = ""
        if self._workspace_ctx is not None:
            cwd = str(self._workspace_ctx.current)
            home = str(self._workspace_ctx.home)
        is_home = cwd == home if cwd else True
        return web.json_response({"cwd": cwd, "home": home, "is_home": is_home})

    async def _handle_workspace_browse(self, request: web.Request) -> web.Response:
        """GET /api/workspace/browse?path=<dir> -- list directory contents."""
        raw = request.query.get("path", "")
        target = Path(raw).expanduser() if raw else Path.home()

        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve(strict=False)
        if not target.is_dir():
            target = Path.home()

        entries: list[dict[str, object]] = []
        try:
            for child in sorted(target.iterdir()):
                try:
                    is_dir = child.is_dir()
                except OSError:
                    continue
                if not is_dir and not child.is_file():
                    continue
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": is_dir,
                })
        except PermissionError:
            pass
        entries.sort(key=lambda e: (not bool(e["is_dir"]), str(e["name"]).lower()))
        parent_path = str(target.parent) if target.parent != target else ""

        drives: list[dict[str, object]] = []
        if target == target.parent:
            import platform
            import string
            if platform.system() == "Windows":
                from pathlib import Path as _P
                for letter in string.ascii_uppercase:
                    drive = _P(f"{letter}:\\")
                    if drive.exists():
                        drives.append({
                            "name": f"{letter}:",
                            "path": str(drive),
                            "is_dir": True,
                        })

        return web.json_response({
            "path": str(target),
            "parent": parent_path,
            "entries": entries,
            "drives": drives,
        })

    async def _handle_workspace_cd(self, request: web.Request) -> web.Response:
        """POST /api/workspace/cd -- change current workspace directory."""
        if self._workspace_ctx is None:
            return web.json_response(
                {"success": False, "cwd": "", "notice": "Workspace not configured"},
                status=503,
            )
        target: str = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw = body.get("path", "")
                if isinstance(raw, str):
                    target = raw.strip()
        except Exception:
            pass
        if not target:
            target = str(self._workspace_ctx.home)
        result = await self._workspace_ctx.cd(target)
        if result.success and self._recent_workspaces is not None:
            self._recent_workspaces.add(str(result.current_path))
        return web.json_response({
            "success": result.success,
            "cwd": str(result.current_path),
            "notice": result.notice,
        })

    async def _handle_workspace_recent(self, request: web.Request) -> web.Response:
        """GET /api/workspace/recent -- return recently visited workspace paths."""
        if self._recent_workspaces is None:
            return web.json_response({"recent": []})
        return web.json_response({
            "recent": self._recent_workspaces.list_recent(),
        })

    async def _handle_create_session(self, request: web.Request) -> web.Response:
        """POST /api/sessions -- create a new session.

        Optional JSON body: ``{"pool": "pool_name"}``.
        """
        pool_name: str | None = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw_pool = body.get("pool")
                if isinstance(raw_pool, str) and raw_pool:
                    pool_name = raw_pool
        except Exception:
            pass

        effective_pool = pool_name or _DEFAULT_AGENT_NAME
        agent_name = (
            self._agent_resolver(effective_pool)
            if self._agent_resolver is not None
            else effective_pool
        )
        if self._session_factory is not None:
            session = self._session_factory.create(agent_name)
            session_id = str(session)
            snowflake = session.snowflake
            created_at = session.created_at
            updated_at = session.updated_at
            if self._session_store is not None:
                await self._session_store.save(session)
        else:
            uuid_prefix = _new_uuid_prefix()
            session_id = f"{uuid_prefix}.{agent_name}"
            snowflake = uuid_prefix
            created_at = None
            updated_at = None
        set_conv_channel(snowflake, "websocket")
        if self._pool_switch_callback is not None:
            self._pool_switch_callback(snowflake, effective_pool)
        return web.json_response({
            "session_id": session_id,
            "agent_name": agent_name,
            "pool": effective_pool,
            "parent_session_id": None,
            "created_at": created_at,
            "updated_at": updated_at,
        })

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """GET /api/sessions -- list sessions visible in the current workspace.

        Query ``?pool=X`` to filter to a single pool (default: all pools).
        All sessions are listed; the frontend builds the tree from
        ``parent_session_id`` — root nodes have ``parent_session_id: null``.

        Falls back to deriving SessionInfo records from transcript files when
        the session index is empty or incomplete, so legacy workspaces (which
        only have ``.modex/sessions/``) still render existing conversations.
        """
        pool_filter: str | None = request.query.get("pool")
        session_list: list[dict[str, object]] = []
        seen_session_ids: set[str] = set()

        if self._session_store is not None:
            for session in await self._session_store.list_sessions():
                agent_name = session.agent_name
                # Show sessions for any agent that maps to a known pool
                # (main agents, resident subagents, and dynamic subagent types).
                pool = self._pool_for_agent_name(agent_name)
                if pool is None:
                    continue
                if pool_filter and pool != pool_filter:
                    continue
                seen_session_ids.add(str(session))
                entry: dict[str, object] = {
                    "session_id": str(session),
                    "agent_name": agent_name,
                    "pool": pool,
                    "parent_session_id": session.parent_session_id,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "metadata": session.metadata,
                }
                session_list.append(entry)

        # Fallback: derive any sessions that have transcripts but are not yet
        # indexed.  This covers legacy data created before the SessionInfo index
        # existed and lets the user interact with them immediately.
        for session in self._derive_sessions_from_transcripts():
            if str(session) in seen_session_ids:
                continue
            agent_name = session.agent_name
            pool = self._pool_for_agent_name(agent_name)
            if pool is None:
                continue
            if pool_filter and pool != pool_filter:
                continue
            seen_session_ids.add(str(session))
            session_list.append({
                "session_id": str(session),
                "agent_name": agent_name,
                "pool": pool,
                "parent_session_id": session.parent_session_id,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "metadata": session.metadata,
            })

        session_list.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
        return web.json_response(session_list)

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/messages -- load transcript events."""
        session_id: str = request.match_info["session_id"]
        agent_name = await self._resolve_agent(session_id)
        pool = self._pool_of_agent(agent_name)
        events = [e.to_dict() for e in self._active_store(pool).load(session_id)]
        return web.json_response(events)

    async def _handle_delete_session(self, request: web.Request) -> web.Response:
        """DELETE /api/sessions/{session_id} -- delete a session.

        Removes the transcript keyed by the FULL session id.  Because the
        dispatcher keys by session id (not conv+agent), a single delete removes
        exactly one session even when several subagents share a conversation.
        We also sweep every pool of the current workspace to clean up
        transcripts that earlier bugs may have written to the wrong pool dir.
        """
        session_id: str = request.match_info["session_id"]
        agent_name = await self._resolve_agent(session_id)
        pool = self._pool_of_agent(agent_name)
        self._active_store(pool).delete_session(session_id)

        # Delete from session store if available.
        if self._session_store is not None:
            await self._session_store.delete(session_id)

        # Robustness: sweep every pool dir to remove transcripts mis-routed by
        # earlier bugs (when an empty agent-pool map wrote everything to main).
        if self._workspace_index is not None:
            for other_pool in self._workspace_index.pools_in(self._current_workspace()):
                if other_pool == pool:
                    continue
                self._active_store(other_pool).delete_session(session_id)

        return web.json_response({"deleted": session_id})

    # ------------------------------------------------------------------
    # Static fallback
    # ------------------------------------------------------------------

    async def _handle_static_index(self, request: web.Request) -> web.FileResponse:
        """Serve index.html from the static dist directory."""
        assert self._static_dist is not None
        return web.FileResponse(self._static_dist / "index.html")

    async def _handle_no_static(self, request: web.Request) -> web.Response:
        """Return 503 when static files are not configured."""
        return web.Response(status=503, text="WebUI static files not configured")

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint -- handles attach, send_message, new/delete session."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        state = _WsConnectionState()

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._dispatch_ws_message(ws, msg.data, state)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", ws.exception())
        except Exception:
            logger.exception("WebSocket handler error")
        finally:
            await state.cleanup(self._input)

        return ws

    async def _dispatch_ws_message(
        self,
        ws: web.WebSocketResponse,
        raw: str,
        state: _WsConnectionState,
    ) -> None:
        """Parse and dispatch a single WebSocket text message."""
        try:
            data: dict[str, object] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in WebSocket message")
            return

        action = str(data.get("action", ""))

        if action == WebSocketAction.ATTACH:
            await self._ws_attach(ws, data, state)
        elif action == WebSocketAction.SEND_MESSAGE:
            await self._ws_send_message(ws, data, state)
        elif action == WebSocketAction.DELETE_CONVERSATION:
            await self._ws_delete_conversation(ws, data)
        else:
            logger.warning("Unknown WebSocket action: %s", action)

    # -- action handlers -----------------------------------------------------

    async def _ws_attach(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        state: _WsConnectionState,
    ) -> None:
        session_id = str(data.get("session_id", ""))

        # ── New conversation path: frontend sends uuid_prefix + pool ──
        uuid_prefix_raw = str(data.get("uuid_prefix", ""))
        pool_from_client = str(data.get("pool", ""))

        if uuid_prefix_raw and pool_from_client:
            agent_name = (
                self._agent_resolver(pool_from_client)
                if self._agent_resolver is not None
                else pool_from_client
            )
            if self._pool_agent_names and agent_name not in self._pool_agent_names:
                asyncio.create_task(
                    ws.send_json(DeltaEnvelope(
                        session_id=session_id or "",
                        agent_name=agent_name,
                        event_type=WebUIEventType.ERROR.value,
                        pool=pool_from_client,
                        payload={"message": f"unknown pool: {pool_from_client}"},
                    ).to_dict())
                )
                return
            # Deferred creation: empty drafts are NOT persisted — the client's
            # uuid_prefix is used verbatim as the snowflake so the session id
            # (``{uuid_prefix}.{agent}``) stays stable through attach→send.
            # Persistence happens on the first message (_ws_send_message).
            session_id = f"{uuid_prefix_raw}.{agent_name}"
            snowflake = uuid_prefix_raw
            uuid_prefix = uuid_prefix_raw
            explicit_agent = agent_name

            # Defensive: if a transcript already exists for this session_id
            # (reattach of a persisted session that already received a message),
            # routing is already established — attach is idempotent.
            try:
                store = self._active_store(pool_from_client)
                if store is not None and any(True for _ in store.load(session_id)):
                    pass  # Session persisted; attach is idempotent, routing intact.
            except Exception:
                pass
        else:
            if not session_id or "." not in session_id:
                asyncio.create_task(
                    ws.send_json(DeltaEnvelope(
                        session_id=session_id or "",
                        agent_name=_DEFAULT_AGENT_NAME,
                        event_type=WebUIEventType.ERROR.value,
                        payload={"message": "session_id required"},
                    ).to_dict())
                )
                return
            resolved = await self._resolve_session(session_id)
            snowflake = resolved.snowflake
            uuid_prefix = snowflake
            explicit_agent = resolved.agent_name

        # Unregister any previous sessions and cancel their forward tasks.
        await state.cleanup(self._input)

        self._input.register_connection(session_id, ws)
        state.attached_sessions.append(session_id)

        # PoolRouter's session store is the single source of truth for routing.
        if explicit_agent and self._agent_pool_map:
            pool_name = self._agent_pool_map.get(explicit_agent)
            if pool_name and self._pool_switch_callback is not None:
                self._pool_switch_callback(snowflake, pool_name)
        else:
            resolved_pool = (
                self._pool_resolver(uuid_prefix) if self._pool_resolver is not None else None
            )
            pool_name = resolved_pool or _DEFAULT_AGENT_NAME
            if self._pool_switch_callback is not None:
                self._pool_switch_callback(snowflake, pool_name)

        # Proactively register ALL pool agent sessions so deltas from any
        # pool's agent are forwarded to this WebSocket client.
        # Use the already-resolved snowflake (encoded for new conversations,
        # the persisted snowflake for existing sessions) so the derived ids
        # match the transcript/delta-queue keys — do NOT re-encode.
        for agent_name in self._pool_agent_names:
            if agent_name == _DEFAULT_AGENT_NAME:
                continue  # already registered above
            pool_sid = f"{snowflake}.{agent_name}"
            if self._input.get_delta_queue(pool_sid) is None:
                self._input.register_connection(pool_sid, ws)
                state.attached_sessions.append(pool_sid)
                state.forward_tasks.append(
                    asyncio.create_task(self._forward_deltas(pool_sid, ws))
                )

        # Also register subagent sessions found in transcript (for history).
        # These are full session ids (``{conv}.{agent}.{invocation_id}``); each
        # invocation is a distinct session.  ``snowflake`` is the stable
        # conversation prefix used by the transcript store.
        for sub_sid in sorted(self._store.list_sessions_in_conversation(snowflake)):
            sub_obj = SessionInfo.from_str(sub_sid)
            if sub_obj.agent_name in self._pool_agent_names and not sub_obj.is_subagent:
                continue  # main-agent session already handled above
            if self._input.get_delta_queue(sub_sid) is None:
                self._input.register_connection(sub_sid, ws)
                state.attached_sessions.append(sub_sid)
                state.forward_tasks.append(
                    asyncio.create_task(self._forward_deltas(sub_sid, ws))
                )

        # Also register subagent sessions from relation store — these may have
        # been dispatched but not yet written to transcript.
        if self._session_store is not None:
            for parent_sid in list(state.attached_sessions):
                for child_session in await self._session_store.get_children(parent_sid):
                    child_sid = str(child_session)
                    if self._input.get_delta_queue(child_sid) is None:
                        self._input.register_connection(child_sid, ws)
                        state.attached_sessions.append(child_sid)
                        state.forward_tasks.append(
                            asyncio.create_task(self._forward_deltas(child_sid, ws))
                        )

        # Watch for dynamically-created subagent delta queues (created by
        # send_envelope auto-create).  When a new queue appears for a
        # session_id not yet forwarded, start a _forward_deltas task.
        state.forward_tasks.append(
            asyncio.create_task(self._watch_new_queues(ws, state))
        )

        state.forward_tasks.append(
            asyncio.create_task(self._forward_deltas(session_id, ws))
        )

        att_agent = await self._resolve_agent(session_id)
        asyncio.create_task(
            ws.send_json(DeltaEnvelope(
                session_id=session_id,
                agent_name=att_agent,
                event_type=WebUIEventType.ATTACHED.value,
                pool=self._pool_of_agent(att_agent),
            ).to_dict())
        )

    async def _materialize_deferred_session(self, session_id: str) -> None:
        """Persist a deferred (uuid_prefix-prefixed) session on first message.

        Attach creates a provisional id ``{uuid_prefix}.{agent}`` without
        persisting; this materializes it just before the pipeline writes the
        transcript, using ``encode_external_id=False`` so ``uuid_prefix`` is
        the verbatim snowflake — same id, no re-encoding.  Already-persisted
        sessions (reattach, existing conversations) are a no-op.
        """
        if self._session_store is None or self._session_factory is None:
            return
        if await self._session_store.get(session_id) is not None:
            return  # already persisted
        snowflake = snowflake_of(session_id)
        agent = agent_of(session_id, default="unknown")
        session = self._session_factory.create(
            agent_name=agent,
            external_id=snowflake,
            encode_external_id=False,
        )
        if str(session) != session_id:
            # Fallback: snowflake contained a separator or was empty; persist a
            # from_str record so the session list still shows the conversation.
            session = SessionInfo.from_str(session_id)
        await self._session_store.save(session)

    async def _ws_send_message(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        state: _WsConnectionState,
    ) -> None:
        session_id = str(data.get("session_id", ""))
        content = str(data.get("content", ""))
        request_id = str(data.get("_request_id", ""))
        if "." not in session_id or not content:
            return
        # Materialize a deferred draft (created via uuid_prefix+pool attach)
        # on its first message so the session enters the index before the
        # pipeline writes the transcript. Empty drafts are never persisted.
        await self._materialize_deferred_session(session_id)

        # NOTE: DO NOT call _try_intercept_control here.
        # Control slash commands (/pwd, /cd, /exit, /stop) are handled by
        # the IM pipeline (S2 EnvironmentControlStage / S3 SessionControlStage).
        # The WebUI does NOT need these — the workspace panel and sidebar
        # controls already provide the same functionality visually.
        # In WebUI, /pwd etc. correctly reach S6 (SkillParseStage) which
        # rejects them with "builtin_not_supported". That is intentional.

        resolved = await self._resolve_session(session_id)
        uuid_prefix = resolved.snowflake
        explicit_agent = resolved.agent_name

        # Pool resolution is OWNED by S5 (ResolvePoolStage) — it also persists
        # the UI choice into PoolSessionStore so PoolRouter routes correctly.
        # The entry only hands the UI-selected pool (derived from the
        # session_id's agent segment) as explicit_pool; no inline resolution,
        # no _pool_switch_callback call here. (attach still uses the callback.)
        explicit_pool = self._agent_pool_map.get(explicit_agent) if explicit_agent else None

        # The session was already established upstream (attach / create_session).
        # Pass it through so the pipeline reuses str(session) verbatim instead of
        # re-encoding the snowflake (which would break transcript/pool keying).
        pre_resolved = await self._resolve_session(session_id)

        # Run the WebUI sub-pipeline (S4..S8).
        from framework.input_pipeline.envelope import UserInputEnvelope

        envelope = UserInputEnvelope(
            conversation_id=uuid_prefix,
            content=content,
            channel="websocket",
            explicit_pool=explicit_pool,
            pre_resolved_session=pre_resolved,
        )
        result = await self._input_pipeline.handle(envelope, self._input_ctx)

        if result.should_continue():
            # Echo the user message back to the WS client so the frontend
            # can reconcile its optimistic message.
            final = result.envelope()
            full_sid = final.metadata["full_session_id"]
            agent_name = final.metadata["resolved_agent"]
            pool_name = final.metadata["resolved_pool"]
            from bot.webui.events import UserMessageEvent

            event = UserMessageEvent(
                session_id=full_sid, agent_name=agent_name, content=content
            )
            meta: dict[str, object] = {}
            if request_id:
                meta["_request_id"] = request_id
            await ws.send_json(
                DeltaEnvelope.from_event(event, meta, pool=pool_name).to_dict()
            )
        else:
            # Terminate: pipeline consumed the message (e.g. /cd /pwd /exit
            # in WebUI chat which has no S2/S3, or unknown /skill).
            # Surface the reason to the client as an error envelope.
            response = getattr(result, "response", None)
            message = ""
            if isinstance(response, dict):
                message = str(response.get("message", ""))
            pool = explicit_pool or _DEFAULT_AGENT_NAME
            await ws.send_json(DeltaEnvelope(
                session_id=session_id,
                agent_name=explicit_agent or _DEFAULT_AGENT_NAME,
                event_type=WebUIEventType.ERROR.value,
                pool=pool,
                payload={"message": message or f"unsupported command in WebUI chat"},
            ).to_dict())

    async def _ws_delete_conversation(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
    ) -> None:
        session_id = str(data.get("session_id", ""))
        if "." not in session_id:
            return
        resolved = await self._resolve_session(session_id)
        uuid_prefix = resolved.snowflake
        agent_name = resolved.agent_name
        pool = self._pool_of_agent(agent_name)
        self._active_store(pool).delete_conversation(uuid_prefix)
        asyncio.create_task(
            ws.send_json(DeltaEnvelope(
                session_id=session_id,
                agent_name=agent_name,
                event_type=WebUIEventType.CONVERSATION_DELETED.value,
                pool=pool,
            ).to_dict())
        )

    # ------------------------------------------------------------------
    # Delta forwarding
    # ------------------------------------------------------------------

    async def _forward_deltas(
        self, session_id: str, ws: web.WebSocketResponse
    ) -> None:
        """Background task: read DeltaEnvelopes and send as structured JSON."""
        try:
            q = self._input.get_delta_queue(session_id)
            if q is None:
                return
            while True:
                envelope: DeltaEnvelope = await q.get()
                await ws.send_json(envelope.to_dict())
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Delta forwarding error for session %s", session_id)

    async def _watch_new_queues(
        self, ws: web.WebSocketResponse, state: _WsConnectionState
    ) -> None:
        """Periodically check for dynamically-created delta queues and start
        forwarding tasks for any that are not yet being drained.

        Subagent sessions dispatched after the initial attach have their delta
        queues auto-created by ``send_envelope``, but no ``_forward_deltas``
        task is running for them.  This watcher discovers those queues and
        starts forwarding.
        """
        try:
            while True:
                await asyncio.sleep(1.0)
                for session_id in list(self._input._delta_queues):
                    if session_id not in state.attached_sessions:
                        state.attached_sessions.append(session_id)
                        state.forward_tasks.append(
                            asyncio.create_task(self._forward_deltas(session_id, ws))
                        )
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Queue watcher error")
