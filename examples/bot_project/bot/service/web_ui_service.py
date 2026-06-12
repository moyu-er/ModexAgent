"""WebUIService — BotService wired for WebUI access with aiohttp server."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from aiohttp import web

from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.service.core import BotService
from bot.webui.emitter import CompositeEmitter, WebBotEmitter
from bot.webui.server import WebUIServer
from bot.webui.transcript_store import JSONLTranscriptStore
from framework.agents.react.agent import ReActEvent
from framework.core.emitter import ContentEmitter, EmitterConfig
from framework.ioc.configs.app import AppConfig

# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_PORT: int = 8080
_DEFAULT_HOST: str = "0.0.0.0"
_DEFAULT_MODE: str = "pool"
_TRANSCRIPT_SUBDIR: str = "data/webui/transcripts"


class WebUIService(BotService):
    """BotService wired for WebUI access with aiohttp server."""

    def __init__(
        self,
        config_dir: Path,
        *,
        mode: str = _DEFAULT_MODE,
        port: int = _DEFAULT_PORT,
        static_dist: Path | None = None,
    ) -> None:
        from dotenv import load_dotenv

        load_dotenv(config_dir.parent / ".env")

        app_cfg = AppConfig.from_yaml(config_dir / "bot_config.yml")

        # WebSocket adapters (shared between emitter and server)
        self._ws_input = WebSocketInputAdapter()
        self._ws_output = WebSocketOutputAdapter(self._ws_input)

        # Project-level transcript storage (independent of workspace /cd /exit)
        self._transcript_store = JSONLTranscriptStore(
            config_dir.parent / _TRANSCRIPT_SUBDIR
        )

        def emitter_factory(session_id: str) -> CompositeEmitter[ReActEvent]:
            web_emitter = WebBotEmitter(
                output_adapter=self._ws_output,
                session_id=session_id,
                config=EmitterConfig(),
                transcript_store=self._transcript_store,
            )
            return CompositeEmitter(emitters=[web_emitter])

        super().__init__(
            config_dir,
            self._ws_input,
            self._ws_output,
            emitter_factory,
            app_config=app_cfg,
        )

        if static_dist is None:
            dist_path = Path(__file__).resolve().parent.parent / "web" / "dist"
            print(f"[WebUI] static_dist auto-detect: {dist_path} exists={dist_path.exists()}")
            if dist_path.exists():
                static_dist = dist_path

        self._port = port
        self._static_dist = static_dist
        self._server = WebUIServer(self._ws_input, self._transcript_store, static_dist)

    async def start(self) -> None:
        """Start the aiohttp server, then start BotService (pools, router)."""
        runner = web.AppRunner(self._server.app)
        await runner.setup()
        site = web.TCPSite(runner, _DEFAULT_HOST, self._port)
        await site.start()
        print(f"[WebUI] Server started on http://{_DEFAULT_HOST}:{self._port}")

        # Inject pool metadata BEFORE starting pool router so the API
        # endpoints work even if pool startup encounters MCP errors.
        pool_agent_names: list[str] = list(self._pools.keys())
        self._server.set_pool_agent_names(pool_agent_names)
        print(f"[WebUI] Pool agents: {pool_agent_names}")

        # Inject workspace context BEFORE starting pool router (same reason).
        if self.workspace_context is not None:
            self._server.set_workspace_context(self.workspace_context)

        await super().start()

        # Inject pool routing callback after pool router is ready.
        if self.pool_router is not None:
            self._server.set_pool_switch_callback(self.pool_router.set_pool)

    async def stop(self) -> None:
        await super().stop()
