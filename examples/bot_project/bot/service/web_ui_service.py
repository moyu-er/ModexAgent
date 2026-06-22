"""Multi-channel BotService — auto-detects and starts all configured IM adapters.

Reads :mod:`bot.adapters.channels` registry, builds every enabled adapter,
merges inputs via ``FanInInputAdapter``, and fans out agent output via
``CompositeEmitter``.  WebUI (websocket) is always enabled and serves as
the universal observer — all conversations from any channel are visible.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from bot.adapters.fan_in import FanInInputAdapter
from bot.adapters.register_websocket import get_ws_input  # noqa: F401 — ensure import
from bot.service.core import BotService
from bot.service.recent_workspaces import RecentWorkspaces
from bot.service.session_store import WorkspacePoolSessionStore
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import CompositeEmitter
from bot.webui.server import WebUIServer
from framework.agents.react.agent import ReActEvent
from framework.core.emitter import ContentEmitter
from framework.core.session_store import LocalFileSessionStore
from framework.ioc.configs.app import AppConfig
from framework.pipeline.adapters import InputAdapter, OutputAdapter

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_PORT: int = 21800
_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_AGENT_NAME: str = "main"


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
                    logger.warning(
                        "Cannot load adapter registration module %s", module_name
                    )
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
        port: int = _DEFAULT_PORT,
        static_dist: Path | None = None,
    ) -> None:
        from dotenv import load_dotenv

        load_dotenv(config_dir.parent / ".env")

        # ── 1. Config ──────────────────────────────────────────────────
        project_dir = config_dir.parent
        app_cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")

        import yaml

        from framework.ioc.configs.app import _resolve_env_in

        raw_config: dict[str, Any] = _resolve_env_in(
            yaml.safe_load(
                (config_dir / "bot_config.yml").read_text(encoding="utf-8")
            )
            or {}
        )

        # ── 2. Shared transcript store + workspace membership ──────────
        # Stores are created from the project home dir for initial adapter
        # builds; after workspace activation the _transcript_store /
        # _session_store properties delegate to the active Workspace.
        _data_dir_name: str = app_cfg.paths.data_dir_name
        home_data_dir: Path = project_dir / _data_dir_name
        home_sessions: Path = home_data_dir / "sessions"
        home_session_index: Path = home_data_dir / "session_index"
        self._home_sessions_dir: Path = home_sessions

        transcript_store: WorkspaceScopedTranscriptStore = WorkspaceScopedTranscriptStore(
            data_dir_name=_data_dir_name,
        )
        self._transcript_store = transcript_store
        # The emitter factory (register_websocket.py) captures the store at
        # build time in a closure. The _sessions_dir_for_prefix callback
        # resolves the workspace sessions_dir per conversation prefix so the
        # transcript store routes writes to the correct workspace.
        self._emitter_transcript_store: WorkspaceScopedTranscriptStore | None = (
            transcript_store
        )

        # ── 2.5 Session store + registry ───────────────────────────────
        from bot.service.session_store import WorkspacePoolSessionStore
        from framework.core.session_registry import InMemorySessionRegistry

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
        primary_output = channels.ChannelRouterOutputAdapter(
            self._channel_outputs_by_name
        )

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
            from framework.core.session_id import agent_of

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

        self._port = port
        self._static_dist = static_dist
        self._server = WebUIServer(
            _ws_in(),
            transcript_store,
            static_dist,
            data_dir=home_sessions,
            home_sessions_dir=home_sessions,
        )
        self._server.set_data_dir_name(_data_dir_name)

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

    @property
    def _session_store(self) -> WorkspacePoolSessionStore | LocalFileSessionStore | None:
        """Return the home workspace's session index store, falling back to
        the initial store set during ``__init__``.

        After ``initialize`` materializes home this delegates to
        ``self._home_resources.session_index_store``.
        """
        home = self._home_resources
        if home is not None:
            return home.session_index_store
        return self.__dict__.get("_session_store")

    @_session_store.setter
    def _session_store(
        self, value: WorkspacePoolSessionStore | LocalFileSessionStore | None
    ) -> None:
        self.__dict__["_session_store"] = value

    def _build_recent_workspaces(self) -> RecentWorkspaces:
        """Build the project-level recent-workspaces store.

        Uses ``AppConfig.paths.data_dir_name`` so the file lives next to the
        workspace metadata directory even when the default ``.modex`` name is
        overridden in config.
        """
        return RecentWorkspaces(
            self._project_dir / self._app_config.paths.data_dir_name
        )

    def _pool_for_agent(self, agent_name: str) -> str:
        """Return the pool name for *agent_name*, defaulting to ``main``."""
        return self._agent_pool_map.get(agent_name, _DEFAULT_AGENT_NAME)

    async def start(self) -> None:
        """Start aiohttp server, then BotService (pools, router).

        All server callbacks are injected BEFORE the server starts accepting
        connections so attach/send_message requests see a fully configured
        callback list from the first request.
        """
        # ── Inject server callbacks BEFORE aiohttp starts ───────────
        # Use main_agent_name from each pool's config — NOT the pool key.
        # This ensures the frontend only sees main agent events, never
        # subagent (reviewer, scout, query-12306, etc.) entries.
        pool_agent_names: list[str] = [
            pi.main_agent_name for pi in self._pools.values()
        ]
        self._server.set_pool_agent_names(pool_agent_names)
        logger.info("Pool agents: %s", pool_agent_names)

        if self.workspace_stack is not None:
            self._server.set_workspace_control(self.workspace_stack.controller)

        # The shared transcript store physically partitions sessions by
        # (workspace, pool) and serves as the WebUI's partition index.
        self._server.set_workspace_index(self._transcript_store)

        # Pool routing callback — must be set before server accepts
        # connections so _ws_attach and _ws_send_message can route.
        # Complete agent→pool map (main agents + subagent template types).
        # Must include subagent types so _pool_of_agent("reviewer") resolves
        # correctly when loading subagent transcripts and routing WS messages.
        agent_pool_map = self._build_agent_pool_map()
        self._agent_pool_map = agent_pool_map
        self._transcript_store.set_agent_pool_map(agent_pool_map)

        # Map pool_name -> main_agent_name from pool configs.
        # pool_name and main_agent_name may differ; the mapping is explicit.
        _agent_map: dict[str, str] = {
            name: pi.main_agent_name for name, pi in self._pools.items()
        }

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
            self._server.set_agent_resolver(
                lambda pool_name: _agent_map.get(pool_name, pool_name)
            )
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
            from framework.core.session_id import agent_of

            agent = agent_of(session_id, default="main")
            pool = agent_pool_map.get(agent, _DEFAULT_AGENT_NAME)
            parent = parent_ids.get(session_id)
            return SessionMeta(pool=pool, parent_session_id=parent)

        set_session_meta_resolver(_resolve_session_meta)

        # Pass session store to server for session list API enrichment
        self._server.set_session_store(self._session_store)

        # Inject recent workspaces store for the recent-workspaces API.
        # RecentWorkspaces lives in the project home data dir (not per-workspace)
        # and uses the configured data_dir_name, not the MODEX_DATA_DIR env var.
        self._recent_workspaces = self._build_recent_workspaces()
        self._server.set_recent_workspaces(self._recent_workspaces)

        # ── Input pipeline convergence ─────────────────────────────
        from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
        from bot.input_pipeline.context import BotInputContext
        from bot.input_pipeline.stages.skill_parse import PoolSkillManagerRegistry
        from framework.core.session_id import SessionIdFactory

        # Per-pool skill registry backed by each pool's real SkillManager.
        # Skills live under skills/{pool}/{agent}/.  One shared registry serves
        # both pipelines; the XML form is produced by the framework helper.
        known_pools = set(self._pools.keys())
        skill_registry = PoolSkillManagerRegistry(self._pools)

        self._session_factory = SessionIdFactory()
        self._server.set_session_factory(self._session_factory)

        def _build_input_context(inp, *, current_ws_provider=None) -> BotInputContext:
            # Both channels share the same routing/persistence wiring; only the
            # physical queue (enqueue_message) and the control adapter differ.
            # Use the service-level pool_session_store so mappings written here
            # are visible to every workspace's PoolRouter during dispatch.
            pool_store = self._pool_session_store
            if pool_store is None:
                # Should not happen: initialize() sets _pool_session_store
                # before start(). Falling back to the home router's store only
                # stays correct while home's router shares the service store;
                # log loudly so a misconfigured startup is diagnosable.
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
                default_pool=self._app_config.multi_agent.default_pool,
                pool_session_store=pool_store,
                agent_pool_map=agent_pool_map,
                agent_resolver=_agent_resolver,
                transcript_store=self._transcript_store,
                enqueue_message=inp.put_input_message,
                command_adapter=inp,
                session_factory=self._session_factory,
                current_ws_provider=current_ws_provider,
            )

        webui_pipeline = build_webui_pipeline(
            skill_registry=skill_registry, known_pools=known_pools
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
        self._server.set_input_context(_build_input_context(ws_input))

        # ── IM pipeline (QQ, etc.) ─────────────────────────────────
        im_pipeline = build_im_pipeline(
            skill_registry=skill_registry,
            known_pools=known_pools,
            workspace_controller=self.workspace_stack.controller if self.workspace_stack is not None else None,
        )
        for inp in self._channel_inputs:
            if inp.name == "websocket":
                continue  # WebSocket is configured via server.set_input_context above
            im_ctx = _build_input_context(
                inp, current_ws_provider=lambda: inp.current_ws
            )
            raw_out = self._channel_outputs_by_name.get(inp.name)
            inp.configure_input_pipeline(im_pipeline, im_ctx, raw_out)

        # ── Control command interception ────────────────────────────
        # Wired centrally in BotService.start() (called via super().start()
        # below) so IM /stop and the WebUI pause button push CANCEL_TURN
        # through InMemoryControlChannel.

        # ── Start aiohttp server ────────────────────────────────────
        runner = web.AppRunner(self._server.app)
        await runner.setup()
        site = web.TCPSite(runner, _DEFAULT_HOST, self._port)
        await site.start()
        logger.info("WebUI server started on http://%s:%d/webui/", _DEFAULT_HOST, self._port)

        await super().start()

    def _build_agent_pool_map(self) -> dict[str, str]:
        """Complete agent -> pool mapping from pool configs + subagent templates.

        Covers main agents, resident subagents (pool config), and
        dynamic-subagent template types so the transcript dispatcher can route
        every write by agent name alone.
        """
        from framework.multi_agent.template_registry import AgentTemplateRegistry

        # Use the already-loaded AppConfig instead of re-parsing YAML files.
        mapping: dict[str, str] = {
            agent.name: pool_name
            for pool_name, pool_cfg in self._app_config.pools.items()
            for agent in pool_cfg.agents
        }

        # Dynamic-subagent template types per pool.
        try:
            reg = AgentTemplateRegistry(self._project_dir)
        except Exception:
            return mapping
        for pool_name in list(mapping.values()):
            for tmpl in reg.list_templates(pool_name):
                mapping.setdefault(tmpl.agent_type, pool_name)
        return mapping

    async def stop(self) -> None:
        await super().stop()
