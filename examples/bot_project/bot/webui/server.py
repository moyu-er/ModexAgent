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

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.control.routes import (
    CONTROL_HISTORY_PATH,
    CONTROL_SEND_PATH,
    handle_history,
    handle_send,
)
from bot.service.config_controller import ConfigController
from bot.service.model_config import BotModelConfig
from bot.service.pool_config_controller import PoolConfigController
from bot.webui.model_fetch import (
    fetch_provider_models,  # noqa: F401 — re-export; tests monkeypatch bot.webui.server.fetch_provider_models
)
from bot.webui.routes.graph_routes import register_graph_routes
from bot.webui.routes.kb_routes import register_kb_routes
from bot.webui.routes.models import register_models_routes
from bot.webui.routes.pool_config import register_pool_config_routes
from bot.webui.routes.sessions import register_sessions_routes
from bot.webui.routes.websocket import register_websocket_routes
from bot.webui.routes.workspace import register_workspace_routes
from bot.webui.transcript_store import TranscriptStore
from bot.workspace.request_resolver import WorkspaceResolution, resolve_ws_request
from modex_agent.core.session_id import (
    SessionIdFactory,
    SessionInfo,
)
from modex_agent.core.session_store import SessionStore
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.port import WorkspaceControlPort

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from bot.service.session_gc import SessionGarbageCollector
    from bot.workspace.handle import PoolWorkspaceResources

logger = logging.getLogger(__name__)


from bot.webui.types import (  # noqa: F401 — re-exports for backward compatibility
    _DEFAULT_AGENT_NAME,
    _DEFAULT_STATIC_DIST,
    _WEBUI_STATIC_PREFIX,
    RuntimeStores,
    WorkspaceIndex,
    _materialize_partial_deltas,
    _new_uuid_prefix,
    _safe_send_json,
    _WsConnectionState,
)

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
        self._home_sessions_dir: Path = (
            home_sessions_dir if home_sessions_dir is not None else Path()
        )
        self._data_dir_name: str = ""

        # Session store (WorkspacePoolSessionStore) -- injected by WebUIService.
        # Either a single store (tests, single-workspace) or a factory that
        # builds a fresh store per workspace index_dir (production multi-live).
        self._session_store: SessionStore | None = None
        self._session_store_factory: Callable[[Path], Awaitable[SessionStore]] | None = None
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
        # Loader that re-reads config/model.yml on each GET /api/models so the
        # selector reflects CLI edits (e.g. `modexbot model`) without a restart.
        # Runtime routing still requires restart (CLI prints "restart to apply").
        self._model_config_loader: Callable[[], BotModelConfig | None] | None = None
        # ConfigController -- injected by WebUIService; serves /api/config/{domain}
        # and /api/system/restart. None degrades the endpoints to 503.
        self._config_controller: ConfigController | None = None
        # PoolConfigController -- injected by WebUIService; serves /api/pools,
        # /api/mcp, /api/skills and the per-agent prompt/skills sub-routes. None
        # degrades the endpoints to 503 (matches ConfigController convention).
        self._pool_config_controller: PoolConfigController | None = None
        # SessionGarbageCollector -- injected by WebUIService for cascade
        # session deletion. None until wired (handler delegation is separate).
        self._session_gc = None
        # Backend-aware runtime store resolver: ``async callback(ws_root, pool)
        # -> RuntimeStores``. Injected by WebUIService so the todos/approvals
        # endpoints read from the same backend the agent writes to.
        self._store_resolver: Callable[[Path, str], Awaitable[RuntimeStores]] | None = None
        # Graph workspace resolver — injected after init via
        # ``set_graph_workspace_resolver``. Takes a workspace_id string and
        # returns the PoolWorkspaceResources for that workspace. When
        # ``None``, graph REST handlers return 503.
        self._graph_workspace_resolver: Callable[[str], PoolWorkspaceResources | None] | None = None

        # Lazy-shared aiohttp ClientSession for outbound provider model-list
        # fetches. Lifecycle owned by :mod:`bot.webui.routes.models`.
        self._http_session: ClientSession | None = None

        self.app = web.Application()
        # Control facade slot — injected by WebUIService via
        # :meth:`set_control_facade`. ``None`` degrades the control routes
        # to 503 (matches ConfigController / PoolConfigController convention).
        self.app["control_facade"] = None
        self._setup_routes()

    # ------------------------------------------------------------------
    # Workspace helpers
    # ------------------------------------------------------------------

    def _resolve_ws_request(self, ws_raw: str) -> WorkspaceResolution:
        """Delegate workspace-root resolution to the shared request resolver.

        Single source of truth for workspace-root resolution, shared by every
        read AND write path (session index, transcript sessions dir, the
        pipeline's bound workspace root) so a message written under a workspace
        is always read back from the same workspace.

        ``relative_base`` is the workspace control's home when wired; when
        ``None`` (minimal test wiring), relative paths resolve against the
        process CWD via ``Path.resolve`` — preserves the prior behavior.
        """
        return resolve_ws_request(
            ws_raw=ws_raw,
            home_root=self._home_sessions_dir.parent.parent,
            relative_base=(
                self._workspace_control.home if self._workspace_control is not None else None
            ),
        )

    def _ws_root_of(self, ws_raw: str) -> Path:
        """Resolve a ws ("ws" == workspace) value to its ROOT directory.

        Delegates to :func:`bot.workspace.request_resolver.resolve_ws_request`.
        Empty -> home root; relative -> against workspace-control home (or CWD
        when no control is wired); absolute -> used as-is. Falls back to the
        home root on any resolution error.

        Note: the ``_sessions_dir_of_ws`` / ``_index_dir_of_ws`` readers
        short-circuit home to the precomputed ``_home_sessions_dir`` so home
        never depends on ``_data_dir_name`` being set; this method is the
        fallback for the home ROOT (e.g. the pipeline's bound workspace root).
        """
        return self._resolve_ws_request(ws_raw).root

    def _sessions_dir_of_ws(self, ws_raw: str) -> Path:
        """Resolve the raw ws path to the sessions directory (transcripts).

        Home (empty ``ws_raw``) returns the canonical ``_home_sessions_dir``;
        a non-home workspace resolves to ``<root>/<data_dir>/sessions`` via the
        shared resolver's derivation helper.
        """
        resolution = self._resolve_ws_request(ws_raw)
        if resolution.is_home:
            return self._home_sessions_dir
        try:
            return resolution.sessions_dir(self._data_dir_name)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build sessions dir for %r: %s", ws_raw, exc)
            return self._home_sessions_dir

    def _index_dir_of_ws(self, ws_raw: str) -> Path:
        """Resolve the raw ws path to the session-INDEX directory.

        Mirrors :meth:`_sessions_dir_of_ws` but for the ``session_index`` layer,
        so the session index is read/written per-workspace (no cross-ws leakage).
        """
        home_index = WorkspacePaths(root=self._home_sessions_dir.parent).session_index_dir
        resolution = self._resolve_ws_request(ws_raw)
        if resolution.is_home:
            return home_index
        try:
            return resolution.session_index_dir(self._data_dir_name)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build session-index dir for %r: %s", ws_raw, exc)
            return home_index

    def _media_dir_of_ws(self, ws_raw: str, pool: str) -> Path:
        """Resolve the raw ws path to the pool's MEDIA directory.

        Thin delegate to :func:`bot.webui.routes.workspace.media_dir_of_ws`
        (extracted in S04). Kept so cross-module callers in
        :mod:`bot.webui.routes.sessions` (``handle_download_attachment`` /
        ``handle_upload_attachment``) continue to read through the server
        instance, matching the existing access pattern.
        """
        from bot.webui.routes.workspace import media_dir_of_ws

        return media_dir_of_ws(self, ws_raw, pool)

    def _media_tmp_dir_of_ws(self, ws_raw: str, pool: str) -> Path:
        """Resolve the raw ws path to the pool's media ``_tmp`` directory.

        Thin delegate to :func:`bot.webui.routes.workspace.media_tmp_dir_of_ws`
        (extracted in S04). Kept so :mod:`bot.webui.routes.sessions`
        ``handle_upload_attachment`` continues to read through the server
        instance.
        """
        from bot.webui.routes.workspace import media_tmp_dir_of_ws

        return media_tmp_dir_of_ws(self, ws_raw, pool)

    def sweep_media_tmp_orphans(self) -> None:
        """Delete leftover upload temp files from a previous run.

        Thin delegate to :func:`bot.webui.routes.workspace.sweep_media_tmp_orphans`
        (extracted in S04). Kept so :class:`bot.service.web_ui_service.WebUIService`
        (``self._server.sweep_media_tmp_orphans()`` at startup) and
        ``tests/webui/test_server_attachment_endpoints.py``
        (``server.sweep_media_tmp_orphans()``) continue to work without change.
        """
        from bot.webui.routes.workspace import sweep_media_tmp_orphans

        sweep_media_tmp_orphans(self)

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

    def set_session_gc(self, gc: SessionGarbageCollector | None) -> None:
        """Inject the SessionGarbageCollector for cascade session deletion."""
        self._session_gc = gc

    def set_store_resolver(
        self,
        resolver: Callable[[Path, str], Awaitable[RuntimeStores]] | None,
    ) -> None:
        """Inject a backend-aware runtime store resolver.

        The resolver returns a :class:`RuntimeStores` for a given workspace
        root + pool. When set, the todos and approvals endpoints use these
        stores instead of the hardcoded file-based stores, so they read from
        the same backend the agent writes to.
        """
        self._store_resolver = resolver

    def set_graph_workspace_resolver(
        self,
        resolver: Callable[[str], PoolWorkspaceResources | None] | None,
    ) -> None:
        """Inject the graph workspace resolver for the graph REST API.

        The resolver takes a workspace_id string and returns the
        ``PoolWorkspaceResources`` for that workspace, from which graph
        route handlers read ``graph_orchestrator`` and ``graph_event_store``.
        """
        self._graph_workspace_resolver = resolver

    def set_workspace_index(self, index: WorkspaceIndex) -> None:
        """Inject the session→workspace membership index."""
        self._workspace_index = index

    def set_session_store(self, store: SessionStore) -> None:
        """Inject a single session store for SessionInfo-based operations.

        Used by tests and single-workspace setups. Production multi-live
        wiring should use :meth:`set_session_store_factory` instead so each
        workspace gets a fresh store rooted at its own session-index dir.
        """
        self._session_store = store

    def set_session_store_factory(self, factory: Callable[[Path], Awaitable[SessionStore]]) -> None:
        """Inject a factory that builds a per-workspace session store.

        The factory receives the workspace's session-index directory (resolved
        from the request's ``?ws=``) and returns a :class:`SessionStore`
        rooted there. This replaces the old per-call ``index_dir`` override
        on :class:`SessionStore` methods — workspace isolation now lives in
        store construction, not per-call routing.
        """
        self._session_store_factory = factory

    async def _session_store_for(self, index_dir: Path) -> SessionStore | None:
        """Thin delegate — implementation in :func:`bot.webui.routes.sessions.session_store_for`.

        Kept so the WebSocket handlers (which stay on the server) can resolve
        per-workspace session stores without duplicating the factory logic.
        """
        from bot.webui.routes.sessions import session_store_for

        return await session_store_for(self, index_dir)

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

    def set_model_config_loader(self, loader: Callable[[], BotModelConfig | None]) -> None:
        """Inject a callable that returns the current BotModelConfig for GET /api/models.

        The loader re-reads config/model.yml so the selector reflects CLI model
        edits without a server restart. Only provider/model names are exposed —
        never api_key/url (handled in _handle_models).
        """
        self._model_config_loader = loader

    def set_config_controller(self, controller: ConfigController) -> None:
        """Inject the ConfigController for /api/config/{domain} and /api/system/restart."""
        self._config_controller = controller

    def set_pool_config_controller(self, controller: PoolConfigController) -> None:
        """Inject the PoolConfigController for /api/pools, /api/mcp, /api/skills."""
        self._pool_config_controller = controller

    def set_control_facade(self, facade: object) -> None:
        """Inject the BotControlFacade for ``POST /api/control/history``.

        Stores the facade on ``self.app["control_facade"]`` so the route
        handler in :mod:`bot.control.routes` can reach it. Typed as
        ``object`` to avoid importing :class:`BotControlFacade` at the
        module level (keeps the import graph lean; the handler performs
        the runtime check).
        """
        self.app["control_facade"] = facade

    # ------------------------------------------------------------------
    # SessionInfo resolution helpers
    # ------------------------------------------------------------------

    async def _resolve_session(self, session_id: str, index_dir: Path | None = None) -> SessionInfo:
        """Thin delegate — implementation in :func:`bot.webui.routes.sessions.resolve_session`."""
        from bot.webui.routes.sessions import resolve_session

        return await resolve_session(self, session_id, index_dir=index_dir)

    async def _resolve_agent(self, session_id: str, index_dir: Path | None = None) -> str:
        """Thin delegate — implementation in :func:`bot.webui.routes.sessions.resolve_agent`."""
        from bot.webui.routes.sessions import resolve_agent

        return await resolve_agent(self, session_id, index_dir=index_dir)

    async def _derive_sessions_from_transcripts(
        self, sessions_dir: Path | None = None
    ) -> list[SessionInfo]:
        """Thin delegate — implementation in :func:`bot.webui.routes.sessions.derive_sessions_from_transcripts`."""
        from bot.webui.routes.sessions import derive_sessions_from_transcripts

        return await derive_sessions_from_transcripts(self, sessions_dir=sessions_dir)

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        """Register REST and WebSocket routes on the aiohttp Application."""
        # Models / config / restart / model-fetch routes (extracted to
        # :mod:`bot.webui.routes.models`). Also sets ``app["server"]`` so the
        # route handlers can reach server state, and appends the http-session
        # cleanup callback.
        register_models_routes(self)
        # Workspace routes (extracted to :mod:`bot.webui.routes.workspace`).
        register_workspace_routes(self)
        # Sessions / messages / todos / approvals / attachments routes
        # (extracted to :mod:`bot.webui.routes.sessions`).
        register_sessions_routes(self)
        # Pool / MCP / skills / prompt REST API (Phase 2B, extracted to
        # :mod:`bot.webui.routes.pool_config`). ``app["server"]`` is set
        # by ``register_models_routes`` above.
        register_pool_config_routes(self)
        # WebSocket route (extracted to :mod:`bot.webui.routes.websocket`).
        register_websocket_routes(self)
        # Graph REST API (T13). Each handler resolves workspace resources
        # via ``server._graph_workspace_resolver``; returns 503 when
        # the resolver is not yet injected (matches the degradation pattern
        # for ConfigController / PoolConfigController).
        register_graph_routes(self, self._graph_workspace_resolver)
        register_kb_routes(self)

        # Control API (T04). The handler checks ``app["control_facade"]`` and
        # returns 503 when the facade is not wired (matches the
        # ConfigController / PoolConfigController degradation pattern).
        self.app.router.add_post(CONTROL_HISTORY_PATH, handle_history)

        # Control API send route (T06).
        self.app.router.add_post(CONTROL_SEND_PATH, handle_send)

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

    async def _handle_models(self, request: web.Request) -> web.Response:
        """Backwards-compat delegate for tests that bypass ``__init__``.

        Production routes use :func:`bot.webui.routes.models.handle_models`
        via ``app["server"]``. Tests in ``test_models_endpoint.py`` construct
        a partial instance with ``__new__`` and register ``inst._handle_models``
        on a standalone app, so this delegate seeds ``app["server"]`` and
        forwards to the extracted handler.
        """
        request.app["server"] = self
        from bot.webui.routes.models import handle_models

        return await handle_models(request)

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
    # WebSocket (delegates -- handlers extracted to bot.webui.routes.websocket)
    # ------------------------------------------------------------------

    @staticmethod
    def _queue_belongs_to_connection(attached_sessions: list[str], session_id: str) -> bool:
        """Thin delegate -- implementation in :func:`bot.webui.routes.websocket.streaming._queue_belongs_to_connection`.

        Kept so ``tests/webui/test_ws_partitioning_convergence.py`` (which calls
        ``WebUIServer._queue_belongs_to_connection`` as a static method)
        continues to work without change.
        """
        from bot.webui.routes.websocket.streaming import _queue_belongs_to_connection

        return _queue_belongs_to_connection(attached_sessions, session_id)
