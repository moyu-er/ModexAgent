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
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.adapter_discovery import import_adapter_registration_modules
from bot.webui.emitter import CompositeEmitter
from bot.webui.server import WebUIServer
from bot.webui.workspace_providers import (
    materialize_workspace,
    resolve_runtime_stores,
    session_store_for_index,
    workspace_persistence_for_data_root,
    workspace_transcript_store_for_sessions,
)
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.emitter import ContentEmitter
from modex_agent.core.session_store import SessionStore
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_config.media import MediaConfig
from modex_agent.persistence.config import PersistenceBackend
from modex_agent.pipeline.adapters import InputAdapter, OutputAdapter

if TYPE_CHECKING:
    from bot.input_pipeline.context import BotInputContext
    from bot.scope import BotRecordScope
    from bot.webui.transcript_store import TranscriptStore
    from bot.workspace.handle import PoolWorkspaceResources
    from modex_agent.memory.core.split_stores import MessageStore
    from modex_agent.multi_agent.communication.service import AgentCommunicationService

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

        Delegates to :func:`bot.webui.adapter_discovery.import_adapter_registration_modules`.
        Kept as a thin staticmethod wrapper for backward compatibility with tests
        that call ``WebUIService._import_adapter_registration_modules`` directly.
        """
        import_adapter_registration_modules(channels_module)

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
                lambda sessions_dir: workspace_transcript_store_for_sessions(
                    self.workspace_stack, sessions_dir
                ),
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
            pool_resolver=lambda session: self._resolve_pool_for_session(session.session_id_prefix),
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

        def output_adapter_factory() -> Any:
            return ws_output

        # on_subagent_created: pre-registers the delta queue so subagent
        # streaming output reaches the browser. The actual SessionInfo record
        # is written by the per-workspace registry inside
        # AgentCommunicationService._create_dynamic_subagent; we do NOT write it
        # again here to avoid leaking it into the home workspace.

        async def _on_subagent_created(child_id: str, parent_id: str) -> None:
            self._parent_ids[child_id] = parent_id
            ws_input = get_ws_input()
            ws_input.ensure_queue(child_id)
            ws_input.register_parent(child_id, parent_id)
            from bot.adapters.register_websocket import get_ws_output
            from bot.webui.events import DeltaEnvelope, WebUIEventType
            from modex_agent.core.session_id import agent_of, session_id_prefix_of

            ws_output = get_ws_output()
            child_agent = agent_of(child_id, default="unknown")
            parent_prefix = session_id_prefix_of(parent_id) if parent_id else session_id_prefix_of(child_id)
            pool = self._resolve_pool_for_session(parent_prefix)
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
        from bot.config.mcp_registry import REGISTRY_PATH as _mcp_registry_path  # noqa: N811
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

    async def _session_store_for_index(self, index_dir: Path) -> SessionStore:
        """Delegates to :func:`bot.webui.workspace_providers.session_store_for_index`.

        Kept as a thin method wrapper for backward compatibility with tests
        that call ``service._session_store_for_index(...)`` directly. The
        FILE branch passes ``data_dir_name`` + ``pool_resolver``; the SQLITE
        branch omits them (unused by the module function) to preserve the
        original lazy-attribute access pattern.
        """
        app_config = self._app_config
        assert app_config is not None
        if app_config.persistence.backend is PersistenceBackend.FILE:
            return await session_store_for_index(
                app_config=app_config,
                workspace_stack=None,
                index_dir=index_dir,
                data_dir_name=self._data_dir_name,
                pool_resolver=lambda session: self._resolve_pool_for_session(session.session_id_prefix),
            )
        return await session_store_for_index(
            app_config=app_config,
            workspace_stack=self.workspace_stack,
            index_dir=index_dir,
        )

    def _resolve_pool_for_session(self, session_prefix: str) -> str:
        """Resolve pool for a session_prefix via the authoritative PoolSessionStore.

        Single convergence point for all service-internal pool resolution.
        Returns _DEFAULT_AGENT_NAME when the store has no entry (e.g. fresh
        boot, session not yet routed).
        """
        if self._pool_session_store is not None:
            pool = self._pool_session_store.get(session_prefix, "")
            if pool:
                return pool
        return _DEFAULT_AGENT_NAME

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

        from bot.service.liveness import DefaultLivenessProvider
        from bot.service.session_cleaner_factory import SessionCleanerFactory
        from bot.service.session_gc import SessionGarbageCollector, load_session_gc_config

        from modex_agent.core.session_id import SessionInfo
        from modex_agent.runtime.store import TurnStateStore

        def _resolve_ws_resources(ws_root: Path) -> PoolWorkspaceResources | None:
            resolved = Path(ws_root).resolve()
            if self._home_resources is not None:
                if Path(self._home_resources.target).resolve() == resolved:
                    return self._home_resources
            if self.workspace_stack is not None:
                for resources in self.workspace_stack.registry.iter_materialized_resources():
                    if Path(resources.target).resolve() == resolved:
                        return resources
            return None

        async def _turn_store_resolver(
            session_id: str, workspace_root: Path
        ) -> TurnStateStore | None:
            resources = _resolve_ws_resources(workspace_root)
            if resources is None:
                return None
            prefix = SessionInfo.from_str(session_id).session_id_prefix
            pool = self._resolve_pool_for_session(prefix)
            pool_data = resources.pool_data.get(pool)
            return pool_data.turn_store if pool_data is not None else None

        liveness_provider = DefaultLivenessProvider(
            turn_store_resolver=_turn_store_resolver,
        )

        gc_cfg = load_session_gc_config(self._raw_config)
        self._session_gc = SessionGarbageCollector(
            workspace_roots_provider=self._workspace_roots_provider,
            data_dir_name=app_config.paths.data_dir_name,
            config=gc_cfg,
            cleaner_factory=SessionCleanerFactory(
                backend=app_config.persistence.backend,
                persistence_resolver=lambda data_root: workspace_persistence_for_data_root(
                    self._home_resources, self.workspace_stack, data_root
                ),
            ),
            transcript_store=self._transcript_store,
            session_store_resolver=self._session_store_for_index,
            session_pool_resolver=lambda session: self._resolve_pool_for_session(session.session_id_prefix),
            liveness_provider=liveness_provider,
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
        # Pool is resolved via PoolSessionStore (session_prefix → pool),
        # the authoritative persisted mapping written by S5 ResolvePoolStage.
        # No agent_name → pool reverse-engineering.
        _agent_map: dict[str, str] = {name: pi.main_agent_name for name, pi in self._pools.items()}

        def _agent_resolver(pool_name: str) -> str:
            return _agent_map.get(pool_name, pool_name)

        if self.pool_router is not None:
            self._server.set_pool_switch_callback(self.pool_router.set_pool)
            self._server.set_pool_resolver(
                lambda conv_id: self.pool_router._session_store.get(conv_id, "") or None
            )
            self._server.set_agent_resolver(lambda pool_name: _agent_map.get(pool_name, pool_name))
            logger.info("Pool routing callback injected (pools=%s)", pool_agent_names)
        else:
            logger.warning("pool_router is None — pool routing disabled")

        # Inject the per-session business routing resolver (pool,
        # parent_session_id) so emitters attach real context to every envelope.
        # pool comes from the authoritative agent→pool map; parent_session_id
        # is resolved from the relation store (persisted or derived fallback).
        from bot.adapters.register_websocket import set_session_meta_resolver
        from bot.webui.events import SessionMeta

        # ── Inject resolver with real parent_session_id ──────────────
        parent_ids = self._parent_ids

        def _resolve_session_meta(session_id: str) -> SessionMeta:
            from modex_agent.core.session_id import session_id_prefix_of

            prefix = session_id_prefix_of(session_id)
            pool = self._resolve_pool_for_session(prefix)
            parent = parent_ids.get(session_id)
            return SessionMeta(pool=pool, parent_session_id=parent)

        set_session_meta_resolver(_resolve_session_meta)

        self._server.set_session_store_factory(self._session_store_for_index)

        # Inject a backend-aware runtime store resolver so the todos and
        # approvals endpoints read from the same backend the agent writes to
        # (SqliteTodoStore / SqliteTurnStateStore in SQLite mode). In FILE
        # mode the resolver returns None stores, and the endpoints fall back
        # to their hardcoded file-based stores.
        self._server.set_store_resolver(
            lambda ws_root, pool: resolve_runtime_stores(
                self.workspace_stack, self._app_config, ws_root, pool
            )
        )

        # Inject the graph workspace resolver (T13 wiring) so the graph REST
        # API can resolve workspace_id → PoolWorkspaceResources. Sync resolver:
        # searches already-materialized resources (home is always materialized
        # at boot via BotService.initialize). Non-home workspaces not yet
        # materialized return None — the graph routes return 503 in that case.
        def _resolve_graph_workspace(ws_id: str) -> PoolWorkspaceResources | None:
            if not ws_id:
                return self._home_resources
            if self.workspace_stack is not None:
                root = self._server._ws_root_of(ws_id)
                resolved = Path(root).resolve()
                for resources in self.workspace_stack.registry.iter_materialized_resources():
                    if Path(resources.target).resolve() == resolved:
                        return resources
            return None

        self._server.set_graph_workspace_resolver(_resolve_graph_workspace)

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
                current_ws_provider=lambda inp=inp: inp.current_ws,
            )
            raw_out = self._channel_outputs_by_name.get(inp.name)
            inp.configure_input_pipeline(im_pipeline, im_ctx, raw_out)

        # ── Control command interception ────────────────────────────
        # Wired centrally in BotService.start() (called via super().start()
        # below) so IM /stop and the WebUI pause button push CANCEL_TURN
        # through InMemoryControlChannel.

        # ── Reclaim leftover upload temp files before serving ────────
        self._server.sweep_media_tmp_orphans()

        # ── Control facade (T04–T08 production wiring) ───────────────
        # BotControlFacade orchestrates POST /api/control/{history,send}.
        # Constructed with production provider callbacks that navigate the
        # materialized PoolWorkspaceResources to reach the per-session
        # MessageStore (native react path), the workspace TranscriptStore
        # (external path), and the per-pool AgentCommunicationService
        # (send path). Without this injection app["control_facade"] stays
        # None and the control routes return 503.
        from bot.control.facade import BotControlFacade, ControlFacadeError
        from bot.control.models import ControlError
        from modex_agent.core.scope import MemoryContext, MemoryLayerName, SessionScope

        async def _resolve_workspace_for_control(
            root: Path,
        ) -> PoolWorkspaceResources:
            return await materialize_workspace(self.workspace_stack, root)

        async def _provide_message_store(
            scope: BotRecordScope,
            resources: PoolWorkspaceResources,
        ) -> MessageStore:
            pool_name = scope.pool
            if pool_name is None:
                raise ControlFacadeError(
                    400,
                    ControlError(
                        code="invalid_scope",
                        message="BotRecordScope.pool is None",
                    ),
                )
            pool_data = resources.pool_data.get(pool_name)
            if pool_data is None:
                raise ControlFacadeError(
                    404,
                    ControlError(
                        code="pool_not_found",
                        message=(
                            f"Pool {pool_name!r} is not materialized in "
                            f"workspace {resources.target!s}"
                        ),
                    ),
                )
            memory_system = pool_data.context_manager.memory_system
            if memory_system is None:
                raise ControlFacadeError(
                    500,
                    ControlError(
                        code="memory_system_unavailable",
                        message=(f"Memory system is not configured for pool {pool_name!r}"),
                    ),
                )
            ctx = MemoryContext(session_id=scope.session_id)
            bundle = await memory_system.store_registry.resolve(
                layer=MemoryLayerName.SESSION,
                scope=SessionScope(),
                context=ctx,
            )
            return bundle.messages

        async def _provide_transcript_store(
            resources: PoolWorkspaceResources,
        ) -> TranscriptStore:
            store = resources.workspace_transcript_store
            if store is None:
                raise ControlFacadeError(
                    422,
                    ControlError(
                        code="transcript_store_unavailable",
                        message=("Transcript store is not configured for this workspace"),
                    ),
                )
            return store

        async def _provide_communication_service(
            resources: PoolWorkspaceResources,
            pool_name: str,
        ) -> AgentCommunicationService:
            pool_instance = resources.pools.get(pool_name)
            if pool_instance is None:
                raise ControlFacadeError(
                    404,
                    ControlError(
                        code="pool_not_found",
                        message=(
                            f"Pool {pool_name!r} is not materialized in "
                            f"workspace {resources.target!s}"
                        ),
                    ),
                )
            return pool_instance.communication_service

        control_facade = BotControlFacade(
            workspace_resolver=_resolve_workspace_for_control,
            message_store_provider=_provide_message_store,
            transcript_store_provider=_provide_transcript_store,
            communication_service_provider=_provide_communication_service,
            home_root=self._project_dir,
            relative_base=self._project_dir,
        )
        self._server.set_control_facade(control_facade)

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
        inp: Any,
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
