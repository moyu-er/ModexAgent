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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

from bot.adapters.channels import set_conv_channel
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.webui.events import (
    DeltaEnvelope,
    UserMessageEvent,
    WebSocketAction,
    WebUIEventType,
)
from framework.core.session_id import SessionInfo, SessionIdFactory, agent_of, session_id_prefix_of
from framework.core.session_store import SessionStore
from framework.utils.timezone import get_user_timezone
from framework.workspace.port import WorkspaceControlPort
from framework.workspace.runtime import resolve_workspace_root
from bot.webui.transcript_store import TranscriptStore

logger = logging.getLogger(__name__)


async def _safe_send_json(ws: web.WebSocketResponse, data: dict[str, object]) -> None:
    """Send JSON to *ws*, swallowing errors from a closed/broken connection.

    Used for fire-and-forget notifications (attached/error/conversation_deleted)
    so a send failure does not leak as an unretrieved asyncio task exception.
    The failure is logged so it can be diagnosed if it occurs unexpectedly.
    """
    try:
        await ws.send_json(data)
    except (ConnectionError, RuntimeError) as exc:
        # Connection already closed or message serialisation impossible; the
        # main WebSocket loop will detect the close and clean up.
        logger.warning("WebSocket send_json failed: %s", exc)



@dataclass
class _WsConnectionState:
    """Tracks all sessions and forward tasks bound to one WebSocket connection."""

    attached_sessions: list[str] = field(default_factory=list)
    forward_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    # Set by cleanup() before cancelling tasks so the queue watcher stops
    # appending sessions / spawning forward tasks that would escape cancellation.
    _stopped: bool = False

    async def cleanup(self, input_adapter: WebSocketInputAdapter) -> None:
        """Drain queues, cancel forward tasks, and unregister all sessions."""
        # Signal the queue watcher to stop BEFORE cancelling tasks so it does
        # not append a new session / spawn a forward task between our clear()
        # and task cancellation (which would orphan that task forever).
        self._stopped = True
        # Drain pending deltas first so a cancelling forward task cannot consume
        # messages intended for an old session and forward them to a reused
        # WebSocket connection during re-attach.
        for session_id in self.attached_sessions:
            q = input_adapter.get_delta_queue(session_id)
            if q is not None:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
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


@dataclass
class SessionListEntry:
    """One row of the ``GET /api/sessions`` response.

    A typed view over :class:`SessionInfo` plus the resolved pool name, so the
    session list API serializes a structure rather than a loose dict.
    """

    session_id: str
    agent_name: str
    pool: str
    parent_session_id: str | None
    created_at: int | None
    updated_at: int | None
    metadata: dict[str, Any]


def _entry_from_session(session: SessionInfo, pool: str) -> SessionListEntry:
    """Build a :class:`SessionListEntry` from a stored session + its pool."""
    return SessionListEntry(
        session_id=session.session_id,
        agent_name=session.agent_name,
        pool=pool,
        parent_session_id=session.parent_session_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        metadata=session.metadata,
    )


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
    def store_for(self, sessions_dir: Path, pool: str) -> TranscriptStore:
        """Return the physical transcript store for *sessions_dir* + *pool*."""
        ...

    @abstractmethod
    def pools_in(self, sessions_dir: Path) -> list[str]:
        """Return pool names that exist under *sessions_dir*."""
        ...

    @abstractmethod
    def list_sessions(self, sessions_dir: Path) -> set[str]:
        """Return all session ids under *sessions_dir*."""
        ...

    @abstractmethod
    def sessions_dir_for_session(self, session_id: str) -> Path:
        """Return the resolved sessions directory for *session_id*."""
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
        home_sessions_dir: Path | None = None,
    ) -> None:
        self._input: WebSocketInputAdapter = input_adapter
        # Shared flat transcript store -- same store the agent emitter and IM
        # FanIn write to.  All transcript I/O (read + write) goes through it.
        self._store: TranscriptStore = transcript_store
        self._static_dist: Path | None = static_dist
        self._data_dir: Path | None = data_dir
        self._home_sessions_dir: Path = home_sessions_dir if home_sessions_dir is not None else Path()
        self._data_dir_name: str = ""

        # Session store (WorkspacePoolSessionStore) -- injected by WebUIService.
        self._session_store: SessionStore | None = None
        # SessionIdFactory -- injected by WebUIService for creating new sessions.
        self._session_factory: SessionIdFactory | None = None

        # Workspace control -- injected by WebUIService for the workspace API.
        # A per-conversation WorkspaceControlPort. The HTTP workspace API is
        # single-active (the browser's current workspace), so it drives the
        # port under a global sentinel conversation id.
        self._workspace_control: WorkspaceControlPort | None = None
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

    def _ws_root_of(self, ws_raw: str) -> Path:
        """Resolve a ws ("ws" == workspace) value to its ROOT directory.

        Single source of truth for workspace-root resolution, shared by every
        read AND write path (session index, transcript sessions dir, the
        pipeline's bound workspace root) so a message written under a workspace
        is always read back from the same workspace.

        - Empty string -> home workspace root (canonical home).
        - Relative path -> resolved against the home workspace root.
        - Absolute path -> used as-is.
        Falls back to the home root on any resolution error.

        Note: the ``_sessions_dir_of_ws`` / ``_index_dir_of_ws`` readers
        short-circuit home to the precomputed ``_home_sessions_dir`` so home
        never depends on ``_data_dir_name`` being set; this method is the
        fallback for the home ROOT (e.g. the pipeline's bound workspace root).
        """
        home_root = self._home_sessions_dir.parent.parent
        if not ws_raw:
            return home_root
        base = Path(ws_raw).expanduser()
        if not base.is_absolute() and self._workspace_control is not None:
            base = self._workspace_control.home / base
        try:
            return base.resolve(strict=False)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to resolve workspace path %r: %s", ws_raw, exc)
            return home_root

    def _sessions_dir_of_ws(self, ws_raw: str) -> Path:
        """Resolve the raw ws path to the sessions directory (transcripts).

        Home (empty ``ws_raw``) returns the canonical ``_home_sessions_dir``;
        a non-home workspace resolves to ``<root>/<data_dir>/sessions``.
        """
        from framework.workspace.paths import WorkspacePaths

        if not ws_raw:
            return self._home_sessions_dir
        try:
            return WorkspacePaths(
                root=self._ws_root_of(ws_raw) / self._data_dir_name
            ).sessions_dir
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build sessions dir for %r: %s", ws_raw, exc)
            return self._home_sessions_dir

    def _index_dir_of_ws(self, ws_raw: str) -> Path:
        """Resolve the raw ws path to the session-INDEX directory.

        Mirrors :meth:`_sessions_dir_of_ws` but for the ``session_index`` layer,
        so the session index is read/written per-workspace (no cross-ws leakage).
        """
        from framework.workspace.paths import WorkspacePaths

        home_index = WorkspacePaths(root=self._home_sessions_dir.parent).session_index_dir
        if not ws_raw:
            return home_index
        try:
            return WorkspacePaths(
                root=self._ws_root_of(ws_raw) / self._data_dir_name
            ).session_index_dir
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build session-index dir for %r: %s", ws_raw, exc)
            return home_index

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

    # ------------------------------------------------------------------
    # Late-binding configuration (called by WebUIService after init)
    # ------------------------------------------------------------------

    def set_pool_agent_names(self, names: list[str]) -> None:
        """Set the list of pool agent names for proactive delta registration."""
        self._pool_agent_names = list(names)

    def set_pool_switch_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for setting pool routing: callback(session_prefix, pool_name)."""
        self._pool_switch_callback = callback

    def set_pool_resolver(self, callback: Callable[[str], str | None]) -> None:
        """Set callback for reading current pool: callback(conv_id) -> pool_name."""
        self._pool_resolver = callback

    def set_agent_resolver(self, callback: Callable[[str], str]) -> None:
        """Set callback for resolving pool_name -> main_agent_name."""
        self._agent_resolver = callback

    def set_data_dir_name(self, data_dir_name: str) -> None:
        """Set the data directory name (e.g. '.modex') for workspace path resolution."""
        self._data_dir_name = data_dir_name

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set mapping from main_agent_name -> pool_name for session list labels."""
        self._agent_pool_map = dict(mapping)

    def set_workspace_control(self, control: WorkspaceControlPort) -> None:
        """Inject the WorkspaceControlPort for the workspace API."""
        self._workspace_control = control

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

    async def _resolve_session(
        self, session_id: str, index_dir: Path | None = None
    ) -> SessionInfo:
        """Resolve a SessionInfo from *session_id*.

        Prefers the session store; falls back to ``SessionInfo.from_str()``
        when no store is injected (e.g. basic tests). *index_dir* scopes the
        lookup to a workspace's session index.
        """
        if self._session_store is not None:
            session = await self._session_store.get(session_id, index_dir=index_dir)
            if session is not None:
                return session
        return SessionInfo.from_str(session_id)

    async def _resolve_agent(
        self, session_id: str, index_dir: Path | None = None
    ) -> str:
        """Return the agent name bound to *session_id*.

        Prefers the authoritative session store; falls back to
        ``SessionInfo.from_str()`` when no store is injected.
        """
        session = await self._resolve_session(session_id, index_dir=index_dir)
        return session.agent_name

    def _derive_sessions_from_transcripts(self, sessions_dir: Path | None = None) -> list[SessionInfo]:
        """Build SessionInfo records from transcript files when the session
        index is missing or incomplete.

        Legacy workspaces only have ``.modex/sessions/<pool>/*.jsonl`` files
        and no ``.modex/session_index/``.  This fallback lets the frontend
        list and attach to those sessions without a separate migration step.
        """
        target_dir = sessions_dir if sessions_dir is not None else self._home_sessions_dir
        derived: list[SessionInfo] = []
        for session_id in self._store.list_sessions(target_dir):
            session_prefix = session_id_prefix_of(session_id)
            if session_prefix == session_id:
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
                    for sid in self._store.list_sessions_by_prefix(
                        session_prefix, sessions_dir=target_dir
                    )
                    if sid != session_id and sid.count(".") == 1
                )
                if candidates:
                    parent_session_id = candidates[0]
            updated_at = self._store.last_updated(session_id, sessions_dir=target_dir)
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
        """GET /api/workspace -- return home path, recent workspaces, and timezone."""
        home = str(self._workspace_control.home) if self._workspace_control is not None else ""
        recent: list[dict[str, object]] = []
        if self._recent_workspaces is not None:
            recent = [
                {"path": r.get("path")}
                for r in self._recent_workspaces.list_recent()
                if isinstance(r, dict) and "path" in r
            ]
        return web.json_response({"home": home, "recent": recent, "timezone": str(get_user_timezone())})

    async def _handle_workspace_browse(self, request: web.Request) -> web.Response:
        """GET /api/workspace/browse?path=<dir> -- list directory contents."""
        raw = request.query.get("path", "")
        target = Path(raw).expanduser() if raw else Path.home()

        if not target.is_absolute():
            target = resolve_workspace_root() / target
        target = target.resolve(strict=False)
        if not target.is_dir():
            target = Path.home()

        # The directory walk is pure synchronous I/O — run it off the event
        # loop so one slow/large directory cannot block other requests.
        def _walk(directory: Path) -> tuple[list[dict[str, object]], str, list[dict[str, object]]]:
            entries: list[dict[str, object]] = []
            try:
                for child in sorted(directory.iterdir()):
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
            parent_path = str(directory.parent) if directory.parent != directory else ""

            drives: list[dict[str, object]] = []
            if directory == directory.parent:
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
            return entries, parent_path, drives

        entries, parent_path, drives = await asyncio.to_thread(_walk, target)

        return web.json_response({
            "path": str(target),
            "parent": parent_path,
            "entries": entries,
            "drives": drives,
        })

    async def _handle_workspace_cd(self, request: web.Request) -> web.Response:
        """POST /api/workspace/cd -- change current workspace directory."""
        if self._workspace_control is None:
            return web.json_response(
                {"success": False, "cwd": "", "notice": "Workspace not configured"},
                status=503,
            )
        target: str = ""
        try:
            body = await request.json()
        except Exception as exc:
            logger.warning("Failed to parse workspace/cd JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        if isinstance(body, dict):
            raw = body.get("path", "")
            if isinstance(raw, str):
                target = raw.strip()
        if not target:
            target = str(self._workspace_control.home)
        result = await self._workspace_control.open_workspace(target)  # registers the workspace without mutating the agent_pool_map
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

        Optional JSON body: ``{"pool": "pool_name", "ws": "<workspace path>"}``.
        ``ws`` scopes the new session to a workspace's session index (home when
        absent) so it never leaks into another workspace's listing.
        """
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
        except Exception as exc:
            logger.warning("Failed to parse /api/sessions JSON body: %s", exc)
        index_dir = self._index_dir_of_ws(ws_raw)

        effective_pool = pool_name or _DEFAULT_AGENT_NAME
        agent_name = (
            self._agent_resolver(effective_pool)
            if self._agent_resolver is not None
            else effective_pool
        )
        if self._session_factory is not None:
            session = self._session_factory.create(agent_name)
            session_id = session.session_id
            session_prefix = session.session_id_prefix
            created_at = session.created_at
            updated_at = session.updated_at
            if self._session_store is not None:
                await self._session_store.save(session, index_dir=index_dir)
        else:
            uuid_prefix = _new_uuid_prefix()
            session_id = f"{uuid_prefix}.{agent_name}"
            session_prefix = uuid_prefix
            created_at = None
            updated_at = None
        set_conv_channel(session_prefix, "websocket")
        if self._pool_switch_callback is not None:
            self._pool_switch_callback(session_prefix, effective_pool)
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
        pool_filter: str | None = request.query.get("pool")
        ws_raw = request.query.get("ws", "")
        index_dir = self._index_dir_of_ws(ws_raw)
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        session_list: list[SessionListEntry] = []
        seen_session_ids: set[str] = set()

        if self._session_store is not None:
            for session in await self._session_store.list_sessions(index_dir=index_dir):
                session_id = session.session_id
                # The store reads recursively, so a record may exist in both a
                # legacy flat layout and a pool subdirectory.  De-dup by id so
                # each conversation appears exactly once.
                if session_id in seen_session_ids:
                    continue
                agent_name = session.agent_name
                # Show sessions for any agent that maps to a known pool
                # (main agents, resident subagents, and dynamic subagent types).
                pool = self._pool_for_agent_name(agent_name)
                if pool is None:
                    continue
                if pool_filter and pool != pool_filter:
                    continue
                seen_session_ids.add(session_id)
                session_list.append(_entry_from_session(session, pool))

        # Fallback: derive any sessions that have transcripts but are not yet
        # indexed.  This covers legacy data created before the SessionInfo index
        # existed and lets the user interact with them immediately.
        for session in self._derive_sessions_from_transcripts(sessions_dir):
            session_id = session.session_id
            if session_id in seen_session_ids:
                continue
            agent_name = session.agent_name
            pool = self._pool_for_agent_name(agent_name)
            if pool is None:
                continue
            if pool_filter and pool != pool_filter:
                continue
            seen_session_ids.add(session_id)
            session_list.append(_entry_from_session(session, pool))

        session_list.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return web.json_response([asdict(entry) for entry in session_list])

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/messages -- load transcript events.

        Returns user messages (as-is) and materialized assistant turns
        (synthetic assistant_turn dicts with blocks), merged by timestamp.
        """
        session_id: str = request.match_info["session_id"]
        # HTTP handlers run outside any dispatch turn, so the ctxvar is not
        # bound — resolve the sessions dir explicitly from ?ws=.
        ws_raw = request.query.get("ws", "")
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        index_dir = self._index_dir_of_ws(ws_raw)
        agent_name: str = await self._resolve_agent(session_id, index_dir=index_dir)
        pool: str = self._pool_of_agent(agent_name)
        session_prefix: str = session_id_prefix_of(session_id)

        store = self._store

        user_events: list[dict[str, object]] = [
            e.to_dict()
            for e in store.load_sessions_by_prefix(
                session_prefix, sessions_dir=sessions_dir, pool=pool
            )
            if e.event == "user_message"
        ]

        turns = store.load_materialized_by_prefix(
            session_prefix, sessions_dir=sessions_dir, pool=pool
        )
        assistant_events: list[dict[str, object]] = []
        for t in turns:
            assistant_events.append({
                "event": "assistant_turn",
                "session_id": session_id,
                "agent_name": agent_name,
                "timestamp": t.started_at,
                "turn_id": t.turn_id,
                "blocks": t.blocks,
                "latency_ms": 0,
            })

        result = user_events + assistant_events

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

    async def _handle_delete_session(self, request: web.Request) -> web.Response:
        """DELETE /api/sessions/{session_id} -- delete a session.

        Removes the transcript keyed by the FULL session id.  Because the
        dispatcher keys by session id (not conv+agent), a single delete removes
        exactly one session even when several subagents share a conversation.
        """
        session_id: str = request.match_info["session_id"]
        ws_raw = request.query.get("ws", "")
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        index_dir = self._index_dir_of_ws(ws_raw)
        self._store.delete_session(session_id, sessions_dir=sessions_dir)

        # Delete from session store if available.
        if self._session_store is not None:
            await self._session_store.delete(session_id, index_dir=index_dir)

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
        elif action == WebSocketAction.PAUSE:
            await self._ws_pause(ws, data)
        elif action == WebSocketAction.DELETE_CONVERSATION:
            await self._ws_delete_conversation(ws, data)
        else:
            logger.warning("Unknown WebSocket action: %s", action)

    # -- action handlers -----------------------------------------------------

    async def _ws_pause(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
    ) -> None:
        """Cancel the running turn for the selected session.

        The WebSocket input adapter is configured with the shared control filter,
        so reusing _try_intercept_control("/stop", ...) sends a CANCEL_TURN
        command through InMemoryControlChannel. The interceptors in the active
        pool drain the command and abort the turn.
        """
        session_id = str(data.get("session_id", ""))
        if "." not in session_id:
            return

        ws_raw = str(data.get("ws", ""))
        index_dir = self._index_dir_of_ws(ws_raw)
        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        await self._input._try_intercept_control("/stop", resolved.session_id)

    async def _ws_attach(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        state: _WsConnectionState,
    ) -> None:
        session_id = str(data.get("session_id", ""))

        # The workspace ("ws") the client attached under — scopes every
        # transcript / session-index read in this attach so history and
        # subagent discovery never cross workspace boundaries. Empty == home.
        attach_ws_raw = str(data.get("ws", ""))
        attach_sessions_dir = self._sessions_dir_of_ws(attach_ws_raw)
        attach_index_dir = self._index_dir_of_ws(attach_ws_raw)

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
                await _safe_send_json(ws, DeltaEnvelope(
                    session_id=session_id or "",
                    agent_name=agent_name,
                    event_type=WebUIEventType.ERROR.value,
                    pool=pool_from_client,
                    payload={"message": f"unknown pool: {pool_from_client}"},
                ).to_dict())
                return
            # Deferred creation: empty drafts are NOT persisted — the client's
            # uuid_prefix is used verbatim as the session_prefix so the session id
            # (``{uuid_prefix}.{agent}``) stays stable through attach→send.
            # Persistence happens on the first message (_ws_send_message).
            session_id = f"{uuid_prefix_raw}.{agent_name}"
            session_prefix = uuid_prefix_raw
            uuid_prefix = uuid_prefix_raw
            explicit_agent = agent_name

            # Defensive: if a transcript already exists for this session_id
            # (reattach of a persisted session that already received a message),
            # routing is already established — attach is idempotent.
            try:
                if any(True for _ in self._store.load(session_id, sessions_dir=attach_sessions_dir)):
                    pass  # Session persisted; attach is idempotent, routing intact.
            except Exception as exc:
                logger.warning("Failed to check existing transcript for %s: %s", session_id, exc)
        else:
            if not session_id or "." not in session_id:
                await _safe_send_json(ws, DeltaEnvelope(
                    session_id=session_id or "",
                    agent_name=_DEFAULT_AGENT_NAME,
                    event_type=WebUIEventType.ERROR.value,
                    payload={"message": "session_id required"},
                ).to_dict())
                return
            resolved = await self._resolve_session(session_id, index_dir=attach_index_dir)
            session_prefix = resolved.session_id_prefix
            uuid_prefix = session_prefix
            explicit_agent = resolved.agent_name

        # Unregister any previous sessions and cancel their forward tasks.
        # cleanup() sets state._stopped (to halt the previous watcher); reset
        # it here because this state is being reused for a fresh attach cycle
        # and the new watcher spawned below must run.
        await state.cleanup(self._input)
        state._stopped = False

        self._input.register_connection(session_id, ws)
        state.attached_sessions.append(session_id)

        # PoolRouter's session store is the single source of truth for routing.
        # pool_from_client is the user's explicit choice from the UI dropdown;
        # use it directly as the pool name without going through agent_pool_map
        # (which may not yet be populated in every edge case).
        pool_name = pool_from_client if pool_from_client else None
        if not pool_name and explicit_agent and self._agent_pool_map:
            pool_name = self._agent_pool_map.get(explicit_agent)
        if not pool_name and self._pool_resolver is not None:
            pool_name = self._pool_resolver(uuid_prefix)
        if not pool_name:
            pool_name = _DEFAULT_AGENT_NAME
        if self._pool_switch_callback is not None:
            self._pool_switch_callback(session_prefix, pool_name)
        # Failsafe: if the callback is not wired (edge case during early
        # startup or test setups), write directly through the input context's
        # pool_session_store so the PoolRouter can still read the mapping.
        elif self._input_ctx is not None and self._input_ctx.pool_session_store is not None:
            self._input_ctx.pool_session_store.set(session_prefix, pool_name)

        # Proactively register ALL pool agent sessions so deltas from any
        # pool's agent are forwarded to this WebSocket client.
        # Use the already-resolved session_prefix (encoded for new conversations,
        # the persisted session_prefix for existing sessions) so the derived ids
        # match the transcript/delta-queue keys — do NOT re-encode.
        for agent_name in self._pool_agent_names:
            if agent_name == _DEFAULT_AGENT_NAME:
                continue  # already registered above
            pool_sid = f"{session_prefix}.{agent_name}"
            if self._input.get_delta_queue(pool_sid) is None:
                self._input.register_connection(pool_sid, ws)
                state.attached_sessions.append(pool_sid)
                state.forward_tasks.append(
                    asyncio.create_task(self._forward_deltas(pool_sid, ws))
                )

        # Also register subagent sessions found in transcript (for history).
        # These are full session ids (``{conv}.{agent}.{invocation_id}``); each
        # invocation is a distinct session.  ``session_prefix`` is the stable
        # conversation prefix used by the transcript store.
        for sub_sid in sorted(self._store.list_sessions_by_prefix(session_prefix, sessions_dir=attach_sessions_dir)):
            sub_agent_name = agent_of(sub_sid, default="unknown")
            # Main-agent sessions have exactly two segments ({prefix}.{agent})
            # and were already registered in the pool_agent_names loop above.
            # Subagent invocations have three segments ({prefix}.{agent}.{inv})
            # and must always be registered — even when the invocation_id
            # coincidentally matches a pool agent name, which would confuse
            # ``SessionInfo.from_str``'s rightmost-segment parsing.
            is_main_agent_session = (
                sub_sid.count(".") == 1 and sub_agent_name in self._pool_agent_names
            )
            if is_main_agent_session:
                continue
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
                for child_session in await self._session_store.get_children(parent_sid, index_dir=attach_index_dir):
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

        att_agent = await self._resolve_agent(session_id, index_dir=attach_index_dir)
        await _safe_send_json(ws, DeltaEnvelope(
            session_id=session_id,
            agent_name=att_agent,
            event_type=WebUIEventType.ATTACHED.value,
            pool=self._pool_of_agent(att_agent),
        ).to_dict())

    async def _materialize_deferred_session(
        self, session_id: str, index_dir: Path | None = None
    ) -> None:
        """Persist a deferred (uuid_prefix-prefixed) session on first message.

        Attach creates a provisional id ``{uuid_prefix}.{agent}`` without
        persisting; this materializes it just before the pipeline writes the
        transcript, using ``create_with_prefix`` so ``uuid_prefix`` is
        the verbatim session_prefix — same id, no re-encoding.  Already-persisted
        sessions (reattach, existing conversations) are a no-op. *index_dir*
        scopes the record to the message's workspace session index.
        """
        if self._session_store is None or self._session_factory is None:
            return
        if await self._session_store.get(session_id, index_dir=index_dir) is not None:
            return  # already persisted
        session_prefix = session_id_prefix_of(session_id)
        agent = agent_of(session_id, default="unknown")
        session = self._session_factory.create_with_prefix(
            agent_name=agent,
            prefix=session_prefix,
        )
        if session.session_id != session_id:
            # Fallback: session_prefix contained a separator or was empty; persist a
            # from_str record so the session list still shows the conversation.
            session = SessionInfo.from_str(session_id)
        await self._session_store.save(session, index_dir=index_dir)

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

        # Resolve the target workspace ("ws" == workspace) from the payload up
        # front: every per-workspace store/index call below needs it. Empty ws
        # means the home workspace. Route the bound workspace root through the
        # SAME resolver the read paths use, so a message written here is always
        # read back from the same workspace.
        ws_raw = str(data.get("ws", ""))
        index_dir = self._index_dir_of_ws(ws_raw)
        workspace_path = self._ws_root_of(ws_raw)

        # Materialize a deferred draft (created via uuid_prefix+pool attach)
        # on its first message so the session enters the index before the
        # pipeline writes the transcript. Empty drafts are never persisted.
        await self._materialize_deferred_session(session_id, index_dir=index_dir)

        # NOTE: DO NOT call _try_intercept_control here.
        # Control slash commands (/pwd, /cd, /exit, /stop) are handled by
        # the IM pipeline (S2 EnvironmentControlStage / S3 SessionControlStage).
        # The WebUI does NOT need these — the workspace panel and sidebar
        # controls already provide the same functionality visually.
        # In WebUI, /pwd etc. correctly reach S6 (SkillParseStage) which
        # rejects them with "builtin_not_supported". That is intentional.

        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        uuid_prefix = resolved.session_id_prefix
        explicit_agent = resolved.agent_name

        # Pool resolution is OWNED by S5 (ResolvePoolStage) — it also persists
        # the UI choice into PoolSessionStore so PoolRouter routes correctly.
        # The entry only hands the UI-selected pool (derived from the
        # session_id's agent segment) as explicit_pool; no inline resolution,
        # no _pool_switch_callback call here. (attach still uses the callback.)
        # For main agents the agent name IS the pool name; fall back to
        # explicit_agent directly when agent_pool_map lacks the entry (edge
        # case: map not yet populated during early server startup).
        explicit_pool = (
            self._agent_pool_map.get(explicit_agent) or explicit_agent
        ) if explicit_agent else None

        # The session was already established upstream (attach / create_session).
        # Pass it through so the pipeline reuses session.session_id verbatim
        # instead of re-encoding the session_prefix (which would break
        # transcript/pool keying).  Reuse the already-resolved SessionInfo
        # from above (same args) rather than resolving a second time.
        pre_resolved = resolved

        # Run the WebUI sub-pipeline (S4..S8).
        from bot.input_pipeline.stages.resolve_pool import RoutingMeta
        from framework.input_pipeline.envelope import UserInputEnvelope

        envelope = UserInputEnvelope(
            external_id=uuid_prefix,
            content=content,
            channel="websocket",
            explicit_pool=explicit_pool,
            pre_resolved_session=pre_resolved,
        )
        envelope.metadata[RoutingMeta.WORKSPACE] = str(workspace_path)
        result = await self._input_pipeline.handle(envelope, self._input_ctx)

        if result.should_continue():
            # Echo the user message back to the WS client so the frontend
            # can reconcile its optimistic message.
            final = result.envelope()
            full_sid = final.metadata[RoutingMeta.FULL_SESSION_ID]
            agent_name = final.metadata[RoutingMeta.RESOLVED_AGENT]
            pool_name = final.metadata[RoutingMeta.RESOLVED_POOL]
            from bot.webui.events import UserMessageEvent

            event = UserMessageEvent(
                session_id=full_sid, agent_name=agent_name, content=content
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
            await _safe_send_json(ws, DeltaEnvelope(
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
        ws_raw = str(data.get("ws", ""))
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        index_dir = self._index_dir_of_ws(ws_raw)
        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        uuid_prefix = resolved.session_id_prefix
        agent_name = resolved.agent_name
        pool = self._pool_of_agent(agent_name)
        self._store.delete_sessions_by_prefix(uuid_prefix, sessions_dir=sessions_dir)
        # Also remove session-index records for the whole conversation so
        # subagent invocation files don't linger as orphans (mirrors the
        # single-session delete path below).
        if self._session_store is not None:
            await self._session_store.delete_sessions_by_prefix(uuid_prefix, index_dir=index_dir)
        await _safe_send_json(ws, DeltaEnvelope(
            session_id=session_id,
            agent_name=agent_name,
            event_type=WebUIEventType.CONVERSATION_DELETED.value,
            pool=pool,
        ).to_dict())

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

    @staticmethod
    def _queue_belongs_to_connection(
        attached_sessions: list[str], session_id: str
    ) -> bool:
        """True if *session_id*'s conversation is already owned by this connection.

        Convergence point for ws isolation on the shared WebSocket adapter: the
        adapter multiplexes every workspace/tab through one set of delta queues,
        keyed only by session id. A dynamically-created subagent queue
        (``{conv}.{agent}.{inv}``) belongs to whichever connection attached that
        conversation. We derive that from the connection's own
        ``attached_sessions`` — every attached session shares one conversation
        prefix — so no per-connection ws bookkeeping is needed: claim a queue
        only when its prefix matches a conversation this connection already owns.
        """
        prefix = session_id_prefix_of(session_id)
        return any(session_id_prefix_of(s) == prefix for s in attached_sessions)

    async def _watch_new_queues(
        self, ws: web.WebSocketResponse, state: _WsConnectionState
    ) -> None:
        """Periodically check for dynamically-created delta queues and start
        forwarding tasks for any that are not yet being drained.

        Subagent sessions dispatched after the initial attach have their delta
        queues auto-created by ``send_envelope``, but no ``_forward_deltas``
        task is running for them.  This watcher discovers those queues and
        starts forwarding.

        ws-scoped: only queues whose conversation this connection already owns
        are claimed (see :meth:`_queue_belongs_to_connection`), so a subagent
        stream from one workspace/tab is never bound to another connection.
        """
        try:
            while True:
                await asyncio.sleep(1.0)
                if state._stopped:
                    # cleanup() has started: stop claiming queues so we never
                    # append a session / spawn a task that cleanup just cleared.
                    break
                for session_id in list(self._input._delta_queues):
                    if state._stopped:
                        break
                    if session_id in state.attached_sessions:
                        continue
                    if not self._queue_belongs_to_connection(
                        state.attached_sessions, session_id
                    ):
                        # Belongs to another connection's conversation; let that
                        # connection's own watcher claim it.
                        continue
                    state.attached_sessions.append(session_id)
                    state.forward_tasks.append(
                        asyncio.create_task(self._forward_deltas(session_id, ws))
                    )
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Queue watcher error")
