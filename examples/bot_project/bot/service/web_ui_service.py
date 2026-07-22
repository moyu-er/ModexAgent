"""Multi-channel BotService — auto-detects and starts all configured IM adapters.

Reads :mod:`bot.adapters.channels` registry, builds every enabled adapter,
merges inputs via ``FanInInputAdapter``, and fans out agent output via
``CompositeEmitter``.  WebUI (websocket) is always enabled and serves as
the universal observer — all conversations from any channel are visible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from aiohttp import web

import bot.config.domains.im  # noqa: F401 - registers the 'im' ConfigDomain on import
import bot.config.domains.model  # noqa: F401 - registers the 'model' ConfigDomain on import
from bot.adapters.fan_in import FanInInputAdapter
from bot.adapters.register_websocket import get_ws_input  # noqa: F401 — ensure import
from bot.persistence.transcript import build_transcript_store_resolver
from bot.service.core import BotService
from bot.service.media_store import WorkspaceScopedMediaStore
from bot.service.recent_workspaces import RecentWorkspaces
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import CompositeEmitter
from bot.webui.server import RuntimeStores, WebUIServer
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.session_store import SessionStore
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_config.media import MediaConfig
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter

if TYPE_CHECKING:
    from bot.input_pipeline.context import BotInputContext
    from bot.webui.transcript_store import TranscriptStore
    from modex_agent.persistence.managers import WorkspacePersistenceManager

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_AGENT_NAME: str = "main"


def load_im_sections(im_path: Path) -> dict[str, Any]:
    """Load ``config/im.yml`` and return its sections (``{}`` if absent).

    Merged into ``raw_config`` at boot so adapters read their config from
    ``ctx.raw_config["<im>"]`` without each adapter parsing the file.
    """
    try:
        data = yaml.safe_load(im_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    return data or {}


def _trigger_restart() -> None:
    """Best-effort self-restart: spawn a detached ``modexbot restart`` then exit.

    Cross-platform self-restart from within a request handler is fragile; the
    handler catches failures and tells the UI to run ``modexbot restart``
    manually. We spawn detached then schedule a hard ``os._exit(0)`` slightly
    later so the HTTP response can flush first.

    ``os._exit`` (not ``sys.exit``) is deliberate: this runs on a Timer thread,
    where ``SystemExit`` only terminates the thread, not the process. The hard
    exit therefore skips graceful async teardown (aiohttp runner, PTB shutdown,
    in-flight transcript flushes) — a small data-loss window accepted for the
    local-tool restart UX (the manual fallback avoids it entirely).
    """
    import os
    import subprocess
    import sys
    import threading

    args: list[str] = [sys.executable, "-m", "modexbot", "restart"]
    try:
        if os.name == "nt":
            subprocess.Popen(  # noqa: S603 - trusted modexbot entry point
                args,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            subprocess.Popen(args, start_new_session=True)  # noqa: S603
    finally:
        threading.Timer(0.5, lambda: os._exit(0)).start()


class WebUIService(BotService):
    """Multi-channel bot service — auto-starts all enabled IM adapters.

    Adapters are discovered from :data:`bot.adapters.channels.ADAPTERS`.
    Each adapter provides input, output, and an emitter factory.  Inputs
    are merged; outputs fan out via ``CompositeEmitter`` with per-channel
    filtering so QQ only responds to QQ-originated conversations, etc.
    """

    # This service runs the WebUI; workspace-level transcript/session_index
    # stores are wired by Workspace when webui=True.
    webui: bool = True

    # Unified transcript store instance shared by the WebSocket emitter factory,
    # the server index, and the input pipeline. The _sessions_dir_for_prefix callback
    # provides the current workspace's sessions_dir dynamically per session prefix.
    _emitter_transcript_store: WorkspaceScopedTranscriptStore | None = None

    @staticmethod
    def _import_adapter_registration_modules(channels_module: Any) -> None:
        """Import every ``bot.adapters.register_*`` module to fire @register decorators.

        New IM adapters do not need to be listed here; dropping a
        ``register_<name>.py`` file into ``bot/adapters/`` is enough.
        """
        import importlib.util
        import sys

        adapters_pkg = Path(channels_module.__file__).parent
        for path in sorted(adapters_pkg.glob("register_*.py")):
            module_name = f"bot.adapters.{path.stem}"
            if module_name in sys.modules:
                continue
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    logger.warning("Cannot load adapter registration module %s", module_name)
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception as exc:
                logger.warning(
                    "Adapter registration module %s import failed: %s",
                    module_name,
                    exc,
                )

    def __init__(
        self,
        config_dir: Path,
        *,
        port: int | None = None,
        static_dist: Path | None = None,
    ) -> None:
        from dotenv import load_dotenv

        load_dotenv(config_dir.parent / ".env")

        # ── 1. Config ──────────────────────────────────────────────────
        project_dir = config_dir.parent
        app_cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")

        from modex_agent.ioc.configs.app import _resolve_env_in

        raw_config: dict[str, Any] = _resolve_env_in(
            yaml.safe_load((config_dir / "bot_config.yml").read_text(encoding="utf-8")) or {}
        )

        # Merge IM sections from config/im.yml so adapters read their config
        # from ctx.raw_config["<im>"] without each adapter parsing the file.
        raw_config.update(load_im_sections(config_dir / "im.yml"))
        self._raw_config = raw_config

        # ── 2. Shared transcript store + workspace membership ──────────
        # Stores are created from the project home dir for initial adapter
        # builds; after workspace activation the _transcript_store /
        # _session_store properties delegate to the active Workspace.
        _data_dir_name: str = app_cfg.paths.data_dir_name
        self._data_dir_name: str = _data_dir_name
        home_data_dir: Path = project_dir / _data_dir_name
        home_sessions: Path = home_data_dir / "sessions"
        home_session_index: Path = home_data_dir / "session_index"
        self._home_sessions_dir: Path = home_sessions

        transcript_store: WorkspaceScopedTranscriptStore = WorkspaceScopedTranscriptStore(
            data_dir_name=_data_dir_name,
            store_resolver=build_transcript_store_resolver(
                app_cfg.persistence.backend,
                self._workspace_transcript_store_for_sessions,
            ),
        )
        self._transcript_store = transcript_store
        # Media store mirrors the transcript store: a service-singleton built
        # once from the data_dir_name. Routed by the bound workspace root
        # (in-turn writers) and by explicit media_dir (HTTP readers). Wired into
        # BotInputContext so the ingest stage persists accepted uploads.
        media_store: WorkspaceScopedMediaStore = WorkspaceScopedMediaStore(
            data_dir_name=_data_dir_name,
        )
        self._media_store = media_store
        # The emitter factory (register_websocket.py) captures the store at
        # build time in a closure. The _sessions_dir_for_prefix callback
        # resolves the workspace sessions_dir per conversation prefix so the
        # transcript store routes writes to the correct workspace.
        self._emitter_transcript_store: WorkspaceScopedTranscriptStore | None = transcript_store

        # ── 2.5 Session store + registry ───────────────────────────────
        from bot.service.session_store import WorkspacePoolSessionStore
        from modex_agent.core.session_registry import InMemorySessionRegistry

        session_store: WorkspacePoolSessionStore = WorkspacePoolSessionStore(
            home_session_index,
            pool_resolver=lambda session: self._pool_for_agent(session.agent_name),
            data_dir_name=_data_dir_name,
        )
        self._session_store = session_store
        self._session_registry = InMemorySessionRegistry(store=session_store)
        # Sync cache for parent lookups at emit time (hot path).
        self._parent_ids: dict[str, str] = {}

        # ── 3. Build adapters from registry ────────────────────────────
        # Auto-import all register_*.py modules so @register decorators fire.
        # Adding a new IM adapter only requires dropping a register_<name>.py
        # file into bot/adapters/; no changes to this service are needed.
        from bot.adapters import channels

        self._import_adapter_registration_modules(channels)

        ctx = channels.AdapterBuildContext(
            config_dir=config_dir,
            project_dir=project_dir,
            raw_config=raw_config,
            transcript_store=transcript_store,
        )

        self._channel_inputs: list[InputAdapter] = []
        self._channel_outputs: list[OutputAdapter] = []
        self._channel_outputs_by_name: dict[str, OutputAdapter] = {}
        self._emitter_factories: list[Any] = []
        """Per-channel emitter factories: ``Callable[[str], ContentEmitter]``."""

        for spec in channels.ADAPTERS:
            if not spec.enabled:
                logger.info("Adapter '%s': disabled, skipping", spec.name)
                continue

            try:
                result = spec.build(ctx)
            except Exception as exc:
                logger.warning(
                    "Adapter '%s': build failed (%s: %s), skipping",
                    spec.name,
                    type(exc).__name__,
                    exc,
                )
                continue

            if result is None:
                logger.info("Adapter '%s': build returned None, skipping", spec.name)
                continue

            inp, out, em_factory = result
            self._channel_inputs.append(inp)
            self._channel_outputs.append(out)
            self._channel_outputs_by_name[spec.name] = out
            self._emitter_factories.append(em_factory)
            logger.info("Adapter '%s': registered [OK]", spec.name)

        if not self._channel_inputs:
            raise RuntimeError(
                "No adapters registered. At least the WebSocket adapter must be enabled."
            )

        # ── 4. Fan-in input adapter ────────────────────────────────────
        if len(self._channel_inputs) == 1:
            merged_input: InputAdapter = self._channel_inputs[0]
        else:
            fan_in = FanInInputAdapter()
            for inp in self._channel_inputs:
                fan_in.add_source(inp)
            merged_input = fan_in
        self._merged_input = merged_input

        # ── 5. Unified emitter factory (CompositeEmitter fan-out) ─────
        def emitter_factory(session_id: str) -> CompositeEmitter[ReActEvent]:
            emitters: list[ContentEmitter[ReActEvent]] = [
                ef(session_id) for ef in self._emitter_factories
            ]
            return CompositeEmitter(emitters=emitters)

        # ── 6. Delegate to BotService ──────────────────────────────────
        # merged_input is the single InputAdapter for PoolRouter.
        # Control command output (cd/exit notices), pool switch replies,
        # and pipeline command responses are routed back to the channel
        # that originated the conversation via ChannelRouterOutputAdapter.
        primary_output = channels.ChannelRouterOutputAdapter(self._channel_outputs_by_name)

        # output_adapter_factory: returns WS output adapter so dynamic
        # subagents stream to the browser instead of NullOutputAdapter.
        from bot.adapters.register_websocket import get_ws_output

        ws_output = get_ws_output()
        output_adapter_factory = lambda: ws_output

        # on_subagent_created: pre-registers the delta queue so subagent
        # streaming output reaches the browser. The actual SessionInfo record
        # is written by the per-workspace registry inside
        # AgentCommunicationService._create_dynamic_subagent; we do NOT write it
        # again here to avoid leaking it into the home workspace.

        async def _on_subagent_created(child_id: str, parent_id: str) -> None:
            # child_id is already a full session_id (e.g. "invocation.helper").
            # Sync cache for hot-path parent lookups at emit time.
            self._parent_ids[child_id] = parent_id
            ws_input = get_ws_input()
            ws_input.ensure_queue(child_id)
            # Notify the browser immediately so the new subagent appears in the
            # sidebar tree as soon as it is spawned, rather than waiting for its
            # first streaming event (or for the user to refresh).
            from bot.adapters.register_websocket import get_ws_output
            from bot.webui.events import DeltaEnvelope, WebUIEventType
            from modex_agent.core.session_id import agent_of

            ws_output = get_ws_output()
            child_agent = agent_of(child_id, default="unknown")
            pool = self._agent_pool_map.get(child_agent, _DEFAULT_AGENT_NAME)
            await ws_output.send_envelope(
                DeltaEnvelope(
                    session_id=child_id,
                    agent_name=child_agent,
                    event_type=WebUIEventType.CONVERSATION_CREATED.value,
                    pool=pool,
                    parent_session_id=parent_id or None,
                )
            )

        super().__init__(
            config_dir,
            merged_input,
            primary_output,
            emitter_factory,
            app_config=app_cfg,
            # ── NEW ──────────────────────────────────────────────────
            output_adapter_factory=output_adapter_factory,
            on_subagent_created=_on_subagent_created,
            session_registry=self._session_registry,
            session_store=session_store,
        )

        # ── 7. WebUI server ────────────────────────────────────────────
        if static_dist is None:
            dist_path = project_dir / "bot" / "web" / "dist"
            if dist_path.exists():
                static_dist = dist_path

        from bot.adapters.register_websocket import get_ws_input as _ws_in

        webui_cfg = raw_config.get("webui", {})
        if not isinstance(webui_cfg, dict):
            webui_cfg = {}
        if port is None:
            port = int(webui_cfg.get("port", 21800))
        host = str(webui_cfg.get("host", "0.0.0.0"))

        self._port = port
        self._host = host
        self._static_dist = static_dist
        self._session_gc = None
        self._web_runner: web.AppRunner | None = None
        self._server = WebUIServer(
            _ws_in(),
            transcript_store,
            static_dist,
            data_dir=home_sessions,
            home_sessions_dir=home_sessions,
        )
        # /api/models re-reads model.yml live so CLI model edits appear in the
        # selector without a server restart (runtime routing still needs restart).
        self._server.set_model_config_loader(self._load_bot_model_config_for_listing)
        from bot.service.config_controller import ConfigController

        self._server.set_config_controller(ConfigController(restarter=_trigger_restart))
        self._server.set_data_dir_name(_data_dir_name)
        # Pool/MCP/skills/prompt REST API (Phase 2B). All four stores share the
        # same base dir (the bot project root) and the MCP registry path under
        # ``config/mcp/registry.json``; default_pool comes from BotService.
        # ``PromptStore.DEFAULT_PROMPT_SEED`` is the single canonical default
        # prompt text (no framework-layer duplicate) — passed into ``PoolStore``
        # so ``create_pool`` seeds main-agent prompt md with the canonical text
        # instead of a framework-hardcoded string.
        from bot.config.mcp_registry import REGISTRY_PATH as _mcp_registry_path
        from bot.config.prompt_store import PromptStore
        from bot.config.skills_store import SkillsStore
        from bot.service.pool_config_controller import PoolConfigController
        from modex_agent.multi_agent.pool_config import PoolStore

        self._server.set_pool_config_controller(
            PoolConfigController(
                pool_store=PoolStore(
                    base_dir=project_dir,
                    default_prompt_seed=PromptStore.DEFAULT_PROMPT_SEED,
                ),
                skills_store=SkillsStore(base_dir=project_dir),
                prompt_store=PromptStore(base_dir=project_dir),
                mcp_registry_path=project_dir / _mcp_registry_path,
                restarter=_trigger_restart,
                pool_session_store=self._pool_session_store,
                is_pool_busy=self._is_pool_busy_provider,
            )
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def _transcript_store(self) -> WorkspaceScopedTranscriptStore | None:
        """Return the unified transcript store used by all channels.

        The same store instance is passed to the WebSocket emitter factory at
        init time.  Writes route by the bound workspace root (ctxvar); reads
        pass an explicit ``sessions_dir`` from HTTP handlers.
        """
        return self._emitter_transcript_store

    @_transcript_store.setter
    def _transcript_store(self, value: WorkspaceScopedTranscriptStore | None) -> None:
        self.__dict__["_transcript_store"] = value
        if value is not None:
            self._emitter_transcript_store = value

    def _build_recent_workspaces(self) -> RecentWorkspaces:
        """Build the project-level recent-workspaces store.

        Uses ``AppConfig.paths.data_dir_name`` so the file lives next to the
        workspace metadata directory even when the default ``.modex`` name is
        overridden in config.
        """
        return RecentWorkspaces(self._project_dir / self._app_config.paths.data_dir_name)

    def _workspace_roots_provider(self) -> list[Path]:
        """Home + every known non-home workspace (authoritative full set)."""
        home = self._project_dir
        targets: list[Path] = []
        if self.workspace_stack is not None:
            targets = self.workspace_stack.registry.known_targets()
        return [home, *targets]

    def _workspace_persistence_for_data_root(
        self, data_root: Path
    ) -> WorkspacePersistenceManager | None:
        """Return the live persistence owner for one workspace data root."""
        resources_by_workspace = []
        if self._home_resources is not None:
            resources_by_workspace.append(self._home_resources)
        if self.workspace_stack is not None:
            resources_by_workspace.extend(
                self.workspace_stack.registry.iter_materialized_resources()
            )
        for resources in resources_by_workspace:
            if resources.ctx.paths.root == data_root.resolve():
                return resources.persistence
        return None

    async def _materialize_workspace(self, ws_root: Path) -> Any:
        """Get-or-open + materialize a workspace, returning its resources.

        Shared helper for all WebUI endpoints that need to resolve
        per-workspace stores (session store, transcript store, runtime
        stores). The registry caches materialized resources, so repeated
        calls for the same workspace are cheap.
        """
        workspace_context = await self.workspace_stack.registry.get_or_open(
            ws_root
        )
        return await self.workspace_stack.registry.materialize(workspace_context)

    async def _session_store_for_index(self, index_dir: Path) -> SessionStore:
        app_config = self._app_config
        assert app_config is not None
        if app_config.persistence.backend is PersistenceBackend.FILE:
            return WorkspacePoolSessionStore(
                base_dir=index_dir,
                pool_resolver=lambda session: self._pool_for_agent(session.agent_name),
                data_dir_name=self._data_dir_name,
            )
        resources = await self._materialize_workspace(index_dir.parent.parent)
        return resources.session_index_store

    async def _resolve_runtime_stores(
        self, ws_root: Path, pool: str
    ) -> RuntimeStores:
        """Resolve backend-aware runtime stores for the WebUI endpoints.

        Returns a :class:`RuntimeStores` from the materialized workspace
        resources when in SQLite mode, or an empty ``RuntimeStores()`` in
        FILE mode (endpoints fall back to their hardcoded file-based stores).
        """
        if (
            self._app_config is None
            or self._app_config.persistence.backend is PersistenceBackend.FILE
        ):
            return RuntimeStores()
        # Materialize the workspace on demand (same pattern as
        # _session_store_for_index) so the resolver works even before the
        # first agent turn materializes the workspace.
        resources = await self._materialize_workspace(ws_root)
        # turn_store comes from PoolDataSnapshot (per-pool).
        pool_data = resources.pool_data.get(pool)
        turn_store = pool_data.turn_store if pool_data is not None else None
        # todo_store is built from the workspace persistence manager
        # (it is not on PoolDataSnapshot — it lives only in the tool).
        todo_store = None
        persistence = resources.persistence
        if persistence is not None:
            from bot.scope import BotRecordScope
            from modex_agent.persistence.adapters.todo_store import SqliteTodoStore

            todo_store = SqliteTodoStore(
                persistence.connection, BotRecordScope(pool=pool)
            )
        return RuntimeStores(todo_store=todo_store, turn_store=turn_store)

    async def _workspace_transcript_store_for_sessions(
        self,
        sessions_dir: Path,
    ) -> TranscriptStore:
        """Materialize a workspace and return its configured transcript adapter."""
        resources = await self._materialize_workspace(sessions_dir.parent.parent)
        transcript_store = resources.workspace_transcript_store
        if transcript_store is None:
            raise RuntimeError(
                f"Database transcript persistence is unavailable for {sessions_dir.parent.parent}"
            )
        return transcript_store

    def _pool_for_agent(self, agent_name: str) -> str:
        """Return the pool name for *agent_name*, defaulting to ``main``."""
        return self._agent_pool_map.get(agent_name, _DEFAULT_AGENT_NAME)

    def _is_pool_busy_provider(self, pool_name: str) -> tuple[bool, list[str]]:
        """Check if a pool has agents with active turns (``AgentState.WORKING``)."""
        from modex_agent.multi_agent.state import AgentState

        pool_instance = self._pools.get(pool_name)
        if pool_instance is None:
            return (False, [])
        busy: list[str] = []
        for desc in pool_instance.pool.list_agents():
            if pool_instance.pool.get_status(desc.address.name) == AgentState.WORKING:
                busy.append(desc.address.name)
        return (bool(busy), busy)

    async def start(self) -> None:
        """Start aiohttp server, then BotService (pools, router).

        All server callbacks are injected BEFORE the server starts accepting
        connections so attach/send_message requests see a fully configured
        callback list from the first request.
        """
        app_config = self._app_config
        assert app_config is not None, "AppConfig must be loaded before start"

        # ── Inject server callbacks BEFORE aiohttp starts ───────────
        # Use main_agent_name from each pool's config — NOT the pool key.
        # This ensures the frontend only sees main agent events, never
        # subagent (reviewer, scout, query-12306, etc.) entries.
        pool_agent_names: list[str] = [pi.main_agent_name for pi in self._pools.values()]
        self._server.set_pool_agent_names(pool_agent_names)
        logger.info("Pool agents: %s", pool_agent_names)

        if self.workspace_stack is not None:
            self._server.set_workspace_control(self.workspace_stack.controller)

        from bot.service.session_cleaner_factory import SessionCleanerFactory
        from bot.service.session_gc import SessionGarbageCollector, load_session_gc_config

        gc_cfg = load_session_gc_config(self._raw_config)
        self._session_gc = SessionGarbageCollector(
            workspace_roots_provider=self._workspace_roots_provider,
            data_dir_name=app_config.paths.data_dir_name,
            config=gc_cfg,
            cleaner_factory=SessionCleanerFactory(
                backend=app_config.persistence.backend,
                persistence_resolver=self._workspace_persistence_for_data_root,
            ),
            transcript_store=self._transcript_store,
            session_store_resolver=self._session_store_for_index,
            session_pool_resolver=lambda session: self._pool_for_agent(
                session.agent_name
            ),
        )
        self._server.set_session_gc(self._session_gc)
        await self._session_gc.start()

        # The shared transcript store physically partitions sessions by
        # (workspace, pool) and serves as the WebUI's partition index.
        transcript_index = self._transcript_store
        assert transcript_index is not None
        self._server.set_workspace_index(transcript_index)

        # Pool routing callback — must be set before server accepts
        # connections so _ws_attach and _ws_send_message can route.
        # Complete agent→pool map (main agents + subagent template types).
        # Must include subagent types so _pool_of_agent("reviewer") resolves
        # correctly when loading subagent transcripts and routing WS messages.
        agent_pool_map = self._build_agent_pool_map()
        self._agent_pool_map = agent_pool_map
        transcript_store = self._transcript_store
        assert transcript_store is not None
        transcript_store.set_agent_pool_map(agent_pool_map)

        # Map pool_name -> main_agent_name from pool configs.
        # pool_name and main_agent_name may differ; the mapping is explicit.
        _agent_map: dict[str, str] = {name: pi.main_agent_name for name, pi in self._pools.items()}

        def _agent_resolver(pool_name: str) -> str:
            return _agent_map.get(pool_name, pool_name)

        if self.pool_router is not None:
            self._server.set_pool_switch_callback(self.pool_router.set_pool)
            # PoolRouter session_store is the single source of truth for routing.
            # Resolver returns None when PoolRouter has no entry (fresh boot) so
            # _ws_attach / _ws_send_message can fall back to persisted metadata.
            self._server.set_pool_resolver(
                lambda conv_id: self.pool_router._session_store.get(conv_id, "") or None
            )
            self._server.set_agent_resolver(lambda pool_name: _agent_map.get(pool_name, pool_name))
            self._server.set_agent_pool_map(agent_pool_map)
            logger.info("Pool routing callback injected (pools=%s)", pool_agent_names)
        else:
            logger.warning("pool_router is None — pool routing disabled")
            self._server.set_agent_pool_map(agent_pool_map)

        # Inject the per-session business routing resolver (pool,
        # parent_session_id) so emitters attach real context to every envelope.
        # pool comes from the authoritative agent→pool map; parent_session_id
        # is resolved from the relation store (persisted or derived fallback).
        from bot.adapters.register_websocket import set_session_meta_resolver
        from bot.webui.events import SessionMeta

        # ── Inject resolver with real parent_session_id ──────────────
        parent_ids = self._parent_ids

        def _resolve_session_meta(session_id: str) -> SessionMeta:
            from modex_agent.core.session_id import agent_of

            agent = agent_of(session_id, default="main")
            pool = agent_pool_map.get(agent, _DEFAULT_AGENT_NAME)
            parent = parent_ids.get(session_id)
            return SessionMeta(pool=pool, parent_session_id=parent)

        set_session_meta_resolver(_resolve_session_meta)

        self._server.set_session_store_factory(self._session_store_for_index)

        # Inject a backend-aware runtime store resolver so the todos and
        # approvals endpoints read from the same backend the agent writes to
        # (SqliteTodoStore / SqliteTurnStateStore in SQLite mode). In FILE
        # mode the resolver returns None stores, and the endpoints fall back
        # to their hardcoded file-based stores.
        self._server.set_store_resolver(self._resolve_runtime_stores)

        # Inject recent workspaces store for the recent-workspaces API.
        # RecentWorkspaces lives in the project home data dir (not per-workspace)
        # and uses the configured data_dir_name, not the MODEX_DATA_DIR env var.
        self._recent_workspaces = self._build_recent_workspaces()
        self._server.set_recent_workspaces(self._recent_workspaces)

        # ── Input pipeline convergence ─────────────────────────────
        from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
        from bot.input_pipeline.stages.skill_parse import PoolSkillManagerRegistry
        from modex_agent.core.session_id import SessionIdFactory

        # Per-pool skill registry backed by each pool's real SkillManager.
        # Skills live under skills/{pool}/{agent}/.  One shared registry serves
        # both pipelines; the XML form is produced by the framework helper.
        known_pools = set(self._pools.keys())
        skill_registry = PoolSkillManagerRegistry(self._pools)

        self._session_factory = SessionIdFactory()
        self._server.set_session_factory(self._session_factory)

        webui_pipeline = build_webui_pipeline(
            skill_registry=skill_registry,
            bot_model_config=self._bot_model_config,
        )
        self._server.set_input_pipeline(webui_pipeline)

        # Find the WebSocket input adapter
        ws_input = None
        for inp in self._channel_inputs:
            if inp.name == "websocket":
                ws_input = inp
                break
        if ws_input is None:
            from bot.adapters.register_websocket import get_ws_input

            ws_input = get_ws_input()
        self._server.set_input_context(
            self._build_input_context(ws_input, agent_resolver=_agent_resolver)
        )

        # ── IM pipeline (QQ, etc.) ─────────────────────────────────
        im_pipeline = build_im_pipeline(
            skill_registry=skill_registry,
            known_pools=known_pools,
            workspace_controller=self.workspace_stack.controller
            if self.workspace_stack is not None
            else None,
        )
        for inp in self._channel_inputs:
            if inp.name == "websocket":
                continue  # WebSocket is configured via server.set_input_context above
            im_ctx = self._build_input_context(
                inp,
                agent_resolver=_agent_resolver,
                current_ws_provider=lambda: inp.current_ws,
            )
            raw_out = self._channel_outputs_by_name.get(inp.name)
            inp.configure_input_pipeline(im_pipeline, im_ctx, raw_out)

        # ── Control command interception ────────────────────────────
        # Wired centrally in BotService.start() (called via super().start()
        # below) so IM /stop and the WebUI pause button push CANCEL_TURN
        # through InMemoryControlChannel.

        # ── Reclaim leftover upload temp files before serving ────────
        self._server.sweep_media_tmp_orphans()

        # ── Start aiohttp server ────────────────────────────────────
        self._web_runner = web.AppRunner(self._server.app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, self._host, self._port)
        await site.start()
        logger.info("WebUI server started on http://%s:%d/webui/", self._host, self._port)

        await super().start()

    def _media_config_for_pool(self, pool: str) -> MediaConfig:
        """ADR-0013 §7 per-pool override: each pool's ingest path uses its own
        ``PoolAssemblyDeps.media``. Unknown pool → the default instance. Exposed as a
        method (not a closure) so the production media-wiring has a testable
        seam (architecture rule 5 — the interface is the test surface)."""
        pi = self._pools.get(pool)
        return pi.media if pi is not None else MediaConfig()

    def _build_input_context(
        self,
        inp,
        *,
        agent_resolver: Callable[[str], str],
        current_ws_provider: Callable[[], Path] | None = None,
    ) -> BotInputContext:
        """Build the shared ``BotInputContext`` for a channel (WS or IM).

        Both channels share routing/persistence wiring; only the physical queue
        (``enqueue_message``) and the control adapter differ. Wires
        ``media_store`` + the per-pool ``MediaConfig`` resolver so inbound
        attachments persist (ADR-0013). Extracted from ``start()`` so the
        production media-wiring is unit-testable — a regression guard for the
        formerly-dead inbound path (where ``media_store`` was never passed and
        the ingest stage silently no-op'd)."""
        from bot.input_pipeline.context import BotInputContext

        # Use the service-level pool_session_store so mappings written here are
        # visible to every workspace's PoolRouter during dispatch.
        pool_store = self._pool_session_store
        if pool_store is None:
            # Should not happen: initialize() sets _pool_session_store before
            # start(). Falling back to the home router's store only stays
            # correct while home's router shares the service store; log loudly
            # so a misconfigured startup is diagnosable.
            logger.warning(
                "[pool-routing] _pool_session_store is None when building "
                "input context for channel '%s' — falling back to "
                "pool_router._session_store. Session→pool mappings written "
                "by this channel may be invisible to other workspaces if "
                "that store is not the service-level singleton.",
                inp.name,
            )
            pool_store = self.pool_router._session_store
        return BotInputContext(
            default_pool=self._default_pool_name,
            pool_session_store=pool_store,
            agent_pool_map=self._agent_pool_map,
            agent_resolver=agent_resolver,
            transcript_store=self._transcript_store,
            enqueue_message=inp.put_input_message,
            command_adapter=inp,
            session_factory=self._session_factory,
            current_ws_provider=current_ws_provider,
            media_store=self._media_store,
            media_config_for_pool=self._media_config_for_pool,
            model_choice_registry=self._model_choice_registry,
            available_pools=self._available_pools_provider,
        )

    def _build_agent_pool_map(self) -> dict[str, str]:
        """Complete agent -> pool mapping from PoolSpec + subagent templates."""
        from modex_agent.multi_agent.pool_config import PoolStore

        pool_store = PoolStore(base_dir=self._project_dir)
        mapping: dict[str, str] = {}
        for summary in pool_store.list_pools():
            spec = pool_store.read_pool(summary.name)
            mapping[spec.main.agent_name] = summary.name
            for sub in spec.subagents:
                mapping[sub.agent_name] = summary.name

        return mapping

    def _available_pools_provider(self) -> set[str]:
        """Return the current set of pool names (re-read from disk each call).

        Used by the input pipeline's ``ResolvePoolStage`` to guard the
        zero-pool and stale-pool cases. Reads the same PoolStore the
        PoolConfigController wraps so the WebUI's pool CRUD is reflected
        without a service restart.
        """
        from modex_agent.multi_agent.pool_config import PoolStore

        return {p.name for p in PoolStore(base_dir=self._project_dir).list_pools()}

    async def stop(self) -> None:
        if self._session_gc is not None:
            await self._session_gc.stop()
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
        await super().stop()
