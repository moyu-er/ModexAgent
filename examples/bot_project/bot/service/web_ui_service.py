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
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.emitter import CompositeEmitter
from bot.webui.server import WebUIServer
from framework.agents.react.agent import ReActEvent
from framework.core.emitter import ContentEmitter
from framework.ioc.configs.app import AppConfig
from framework.pipeline.adapters import InputAdapter, OutputAdapter

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_PORT: int = 21800
_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_AGENT_NAME: str = "main"
def _modex_dir(project_dir: Path) -> Path:
    """Data root — ``project_dir / MODEX_DATA_DIR``.

    Used for the home (project) workspace only.  After ``cd``, session
    stores are rebuilt to point at ``{new_workspace}/.modex/``.
    """
    import os

    return project_dir / os.environ.get("MODEX_DATA_DIR", ".modex")


def _sessions_dir(workspace_data_dir: Path) -> Path:
    """Transcript directory under the given workspace data dir.

    After ``cd``, this is rebuilt to point at the new workspace's
    ``.modex/sessions/`` so each workspace maintains its own transcripts.
    """
    return workspace_data_dir / "sessions"


def _session_index_dir(workspace_data_dir: Path) -> Path:
    """SessionId metadata index — flat directory, separate from transcripts.

    Each session has one ``{safe_id}.json`` file.  Workspace switching
    rebases the store root to the new workspace's index directory.
    """
    return workspace_data_dir / "session_index"


class WebUIService(BotService):
    """Multi-channel bot service — auto-starts all enabled IM adapters.

    Adapters are discovered from :data:`bot.adapters.channels.ADAPTERS`.
    Each adapter provides input, output, and an emitter factory.  Inputs
    are merged; outputs fan out via ``CompositeEmitter`` with per-channel
    filtering so QQ only responds to QQ-originated conversations, etc.
    """

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
        # Sessions live per-workspace under {workspace}/.modex/sessions/.
        # At startup use the home (project) workspace; after cd the stores
        # are rebuilt via _update_session_stores() called from the workspace
        # switch callback in core.py.
        sessions_dir = _sessions_dir(_modex_dir(project_dir))
        transcript_store = WorkspaceScopedTranscriptStore(
            sessions_dir,
            self._resolve_workspace,
        )
        self._transcript_store = transcript_store

        # ── 2.5 Session store + registry ───────────────────────────────
        # Flat index under .modex/session_index/ — separate from
        # .modex/sessions/ (transcripts).  One JSON file per SessionId.
        from bot.service.session_store import WorkspacePoolSessionStore
        from framework.core.session_registry import InMemorySessionRegistry

        index_dir = _session_index_dir(_modex_dir(project_dir))
        self._session_store = WorkspacePoolSessionStore(index_dir)
        self._session_registry = InMemorySessionRegistry(store=self._session_store)
        # Sync cache for parent lookups at emit time (hot path).
        self._parent_ids: dict[str, str] = {}

        # ── 2.6 Recent workspaces store ────────────────────────────
        # Lives in the project home .modex/ (not per-workspace) because
        # it tracks which workspaces the user has visited, not data owned
        # by any single workspace.
        from bot.service.recent_workspaces import RecentWorkspaces

        self._recent_workspaces = RecentWorkspaces(_modex_dir(project_dir))

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

        # on_subagent_created: records parent→child relation AND pre-registers
        # the delta queue so subagent streaming output reaches the browser.
        # The watcher in _ws_attach picks up the queue within 1s and starts
        # a _forward_deltas task for it.
        from bot.adapters.register_websocket import get_ws_input

        async def _on_subagent_created(child_id: str, parent_id: str) -> None:
            # Parse agent_name from child_id: {snowflake}.{agent}[.{invocation_id}]
            parts = child_id.split(".", 2)
            agent_name = parts[1] if len(parts) >= 2 else "main"
            from framework.core.session_id import SessionId, now_ms

            child_session = SessionId(
                session_id=child_id,
                agent_name=agent_name,
                parent_session_id=parent_id,
                created_at=now_ms(),
                updated_at=now_ms(),
            )
            await self._session_registry.register(child_session)
            # Sync cache for hot-path parent lookups at emit time.
            self._parent_ids[child_id] = parent_id
            ws_input = get_ws_input()
            ws_input.ensure_queue(child_id)

        super().__init__(
            config_dir,
            merged_input,
            primary_output,
            emitter_factory,
            app_config=app_cfg,
            # ── NEW ──────────────────────────────────────────────────
            output_adapter_factory=output_adapter_factory,
            on_subagent_created=_on_subagent_created,
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
            data_dir=sessions_dir,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _resolve_workspace(self) -> str:
        """Return the currently active workspace path (shared by all channels).

        Lazily read at message-write time so the resolver always reflects the
        latest ``cd``/``exit``.  Returns "" before the workspace context exists.
        """
        ctx = self.workspace_context
        return str(ctx.current) if ctx is not None else ""

    def _pool_for_agent(self, agent_name: str) -> str:
        """Return the pool name for *agent_name*, defaulting to ``main``."""
        return self._agent_pool_map.get(agent_name, _DEFAULT_AGENT_NAME)

    def _pool_for_agent(self, agent_name: str) -> str:
        """Return the pool name for *agent_name*, defaulting to ``main``."""
        return self._agent_pool_map.get(agent_name, _DEFAULT_AGENT_NAME)

    def update_session_stores(self, new_data_dir: Path) -> None:
        """Rebase transcript and session-index stores to *new_data_dir*.

        Called after a workspace switch.  Transcripts go to
        ``.modex/sessions/``; SessionId metadata goes to
        ``.modex/session_index/`` (separate flat tree).  The runtime
        registry cache is cleared and reloaded from the new index.
        """
        new_sessions_dir = _sessions_dir(new_data_dir)
        new_sessions_dir.mkdir(parents=True, exist_ok=True)
        new_index_dir = _session_index_dir(new_data_dir)
        new_index_dir.mkdir(parents=True, exist_ok=True)

        # Rebase transcript store.
        self._transcript_store.rebase(new_sessions_dir)

        # Rebase session index and reload registry cache.
        self._session_store._root = new_index_dir
        self._parent_ids.clear()

        # Reload runtime cache from the new workspace's index (fire-and-forget).
        async def _reload() -> None:
            try:
                await self._session_registry.load_all()
            except Exception:
                logger.exception("Failed to reload session registry after workspace switch")
        asyncio.create_task(_reload())

        # Update server's data_dir if already set.
        if self._server is not None:
            self._server._data_dir = new_sessions_dir

    async def start(self) -> None:
        """Start aiohttp server, then BotService (pools, router).

        All server callbacks are injected BEFORE the server starts accepting
        connections so attach/send_message requests see a fully configured
        callback list from the first request.
        """
        # ── Rebase stores to current workspace (covers initial restore) ─
        # WorkspaceScopedTranscriptStore is created in __init__ with _base
        # pointing at the project home.  If initialize() restored a different
        # workspace from disk, the callback that would normally rebase the
        # store was registered AFTER restore, so _base never moved.  We fix
        # that here — idempotent rebase before the first read or write.
        if self.workspace_context is not None:
            ws_data_dir = self.workspace_context.data_dir
            self.update_session_stores(ws_data_dir)

        # ── Inject server callbacks BEFORE aiohttp starts ───────────
        # Use main_agent_name from each pool's config — NOT the pool key.
        # This ensures the frontend only sees main agent events, never
        # subagent (reviewer, scout, query-12306, etc.) entries.
        pool_agent_names: list[str] = [
            pi.main_agent_name for pi in self._pools.values()
        ]
        self._server.set_pool_agent_names(pool_agent_names)
        print(f"[WebUI] Pool agents: {pool_agent_names}")

        if self.workspace_context is not None:
            self._server.set_workspace_context(self.workspace_context)

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
            print(f"[WebUI] Pool routing callback injected (pools={pool_agent_names})")
        else:
            print("[WebUI] WARNING: pool_router is None — pool routing disabled!")
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
            parts = session_id.split(".", 2)
            agent = parts[1] if len(parts) >= 2 else "main"
            pool = agent_pool_map.get(agent, _DEFAULT_AGENT_NAME)
            parent = parent_ids.get(session_id)
            return SessionMeta(pool=pool, parent_session_id=parent)

        set_session_meta_resolver(_resolve_session_meta)

        # Pass session store to server for session list API enrichment
        self._server.set_session_store(self._session_store)

        # Inject recent workspaces store for the recent-workspaces API
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

        session_factory = SessionIdFactory()
        self._session_factory = session_factory

        self._server.set_session_factory(self._session_factory)
        self._session_factory = session_factory

        self._server.set_session_factory(self._session_factory)

        def _build_input_context(inp) -> BotInputContext:
            # Both channels share the same routing/persistence wiring; only the
            # physical queue (enqueue_message) and the control adapter differ.
            return BotInputContext(
                default_pool=self._app_config.multi_agent.default_pool,
                pool_session_store=self.pool_router._session_store,
                agent_pool_map=agent_pool_map,
                agent_resolver=_agent_resolver,
                transcript_store=self._transcript_store,
                enqueue_message=inp.put_input_message,
                command_adapter=inp,
                session_factory=session_factory,
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
        )
        for inp in self._channel_inputs:
            if inp.name == "websocket":
                continue  # WebSocket is configured via server.set_input_context above
            im_ctx = _build_input_context(inp)
            raw_out = self._channel_outputs_by_name.get(inp.name)
            inp.configure_input_pipeline(im_pipeline, im_ctx, raw_out)

        # ── Start aiohttp server ────────────────────────────────────
        runner = web.AppRunner(self._server.app)
        await runner.setup()
        site = web.TCPSite(runner, _DEFAULT_HOST, self._port)
        await site.start()
        print(f"[WebUI] Server started on http://{_DEFAULT_HOST}:{self._port}/webui/")

        await super().start()

    def _build_agent_pool_map(self) -> dict[str, str]:
        """Complete agent -> pool mapping from pool configs + subagent templates.

        Covers main agents, resident subagents (pool config), and
        dynamic-subagent template types so the transcript dispatcher can route
        every write by agent name alone.
        """
        from framework.multi_agent.template_registry import AgentTemplateRegistry

        mapping: dict[str, str] = {}
        # Read pool configs (config/pools/{pool}.yml).
        pools_dir = self._project_dir / "config" / "pools"
        if pools_dir.is_dir():
            import yaml
            for config_path in sorted(pools_dir.glob("*.yml")):
                pool_name = config_path.stem
                try:
                    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                for agent in (raw.get("agents") or []):
                    mapping[agent["name"]] = pool_name

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
