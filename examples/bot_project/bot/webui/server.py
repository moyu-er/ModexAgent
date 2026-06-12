"""aiohttp WebUI server with REST API and WebSocket support."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable
from uuid import uuid4

from aiohttp import web

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.webui.events import (
    AttachMessage,
    DeleteConversationMessage,
    NewConversationMessage,
    SendMessageMessage,
    UserMessageEvent,
    WebSocketAction,
    WebUIEventType,
)
from bot.webui.transcript_store import TranscriptStore

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_AGENT_NAME: str = "main"
_API_SESSIONS_PATH: str = "/api/sessions"
_API_SESSIONS_CONV_PATH: str = "/api/sessions/{conv_id}"
_API_POOLS_PATH: str = "/api/pools"
_WS_PATH: str = "/ws"
_WEBUI_STATIC_PREFIX: str = "/webui/"
_DEFAULT_STATIC_DIST: Path = Path(__file__).resolve().parent.parent / "web" / "dist"


def _make_session_id(conversation_id: str, agent_name: str = _DEFAULT_AGENT_NAME) -> str:
    """Build a session_id matching DefaultSessionIdStrategy: ``{conv_id}.{agent_name}``."""
    return f"{conversation_id}.{agent_name}"


def _new_conversation_id() -> str:
    """Generate a new conversation ID."""
    return uuid4().hex[:12]


# ── Server ─────────────────────────────────────────────────────────────────


class WebUIServer:
    """HTTP + WebSocket server for the bot WebUI.

    REST endpoints:
        GET  /api/pools                — list available pool names
        GET  /api/sessions             — list all conversations and their agents
        POST /api/sessions             — create a new conversation
        GET  /api/sessions/{conv}/messages?all=true  — load all agents' events
        DELETE /api/sessions/{conv}    — delete a conversation
        GET  /ws                       — WebSocket endpoint for real-time chat

    Static files:
        If ``static_dist`` is provided, files under ``/webui/`` are served
        from that directory.  Otherwise ``/webui/`` returns 503.
    """

    def __init__(
        self,
        input_adapter: WebSocketInputAdapter,
        transcript_store: TranscriptStore,
        static_dist: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._input: WebSocketInputAdapter = input_adapter
        self._store: TranscriptStore = transcript_store
        self._static_dist: Path | None = static_dist

        # Seed conversation IDs from disk so history survives restarts.
        self._conversations: set[str] = set(self._store.list_conversations())
        self._delta_tasks: dict[str, asyncio.Task[None]] = {}

        # Workspace context — injected by WebUIService for the workspace API.
        self._workspace_ctx = None

        # Pool metadata — injected by WebUIService after pool initialization.
        self._pool_agent_names: list[str] = [_DEFAULT_AGENT_NAME]
        self._pool_switch_callback: Callable[[str, str], None] | None = None

        # Per-conversation pool routing (T3: pool per conversation).
        self._data_dir: Path | None = data_dir
        self._conversation_pools: dict[str, str] = {}
        self._pool_mapping_path: Path | None = (
            data_dir / "conversation_pools.json" if data_dir is not None else None
        )
        self._load_pool_mapping()

        self.app = web.Application()
        self._setup_routes()

    # ------------------------------------------------------------------
    # Late-binding configuration (called by WebUIService after init)
    # ------------------------------------------------------------------

    def set_pool_agent_names(self, names: list[str]) -> None:
        """Set the list of pool agent names for proactive delta registration."""
        self._pool_agent_names = list(names)

    def set_pool_switch_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for setting pool routing: callback(session_id, pool_name)."""
        self._pool_switch_callback = callback

    def set_workspace_context(self, ctx) -> None:
        """Inject the WorkspaceContext for workspace API."""
        self._workspace_ctx = ctx

    # ------------------------------------------------------------------
    # Pool mapping persistence (T3: pool per conversation)
    # ------------------------------------------------------------------

    def _load_pool_mapping(self) -> None:
        """Load conversation→pool mapping from JSON file on disk."""
        if self._pool_mapping_path is None or not self._pool_mapping_path.exists():
            return
        try:
            data = json.loads(self._pool_mapping_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._conversation_pools = {
                    str(k): str(v) for k, v in data.items()
                }
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load pool mapping from %s", self._pool_mapping_path)

    def _save_pool_mapping(self) -> None:
        """Persist conversation→pool mapping to JSON file on disk."""
        if self._pool_mapping_path is None:
            return
        try:
            self._pool_mapping_path.parent.mkdir(parents=True, exist_ok=True)
            self._pool_mapping_path.write_text(
                json.dumps(self._conversation_pools, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("Failed to save pool mapping to %s", self._pool_mapping_path)

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        """Register REST and WebSocket routes on the aiohttp Application."""
        self.app.router.add_get(_API_POOLS_PATH, self._handle_pools)
        self.app.router.add_get("/api/workspace", self._handle_workspace)
        self.app.router.add_get(_API_SESSIONS_PATH, self._handle_sessions)
        self.app.router.add_post(_API_SESSIONS_PATH, self._handle_create_session)
        self.app.router.add_get(
            f"{_API_SESSIONS_CONV_PATH}/messages", self._handle_get_messages
        )
        self.app.router.add_delete(_API_SESSIONS_CONV_PATH, self._handle_delete_session)
        self.app.router.add_get(_WS_PATH, self._handle_websocket)

        if self._static_dist is not None:
            # Serve index.html at the root of the webui prefix.
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
        """GET /api/pools — list available pool names."""
        pools: list[dict[str, str]] = [
            {"name": name} for name in sorted(self._pool_agent_names)
        ]
        return web.json_response(pools)

    async def _handle_workspace(self, request: web.Request) -> web.Response:
        """GET /api/workspace — return current workspace path."""
        cwd: str = ""
        home: str = ""
        if self._workspace_ctx is not None:
            cwd = str(self._workspace_ctx.current)
            home = str(self._workspace_ctx.home)
        return web.json_response({"cwd": cwd, "home": home})

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """GET /api/sessions — list all conversations and their agents.

        Only returns conversations that have actual transcript data.
        Empty conversations (created but never messaged) are skipped.
        """
        sessions: list[dict[str, object]] = []
        for conv_id in sorted(self._conversations):
            agents = sorted(self._store.list_agents(conv_id))
            if not agents:
                continue  # skip conversations with no messages
            sessions.append({
                "conversation_id": conv_id,
                "agents": agents,
                "pool": self._conversation_pools.get(conv_id, _DEFAULT_AGENT_NAME),
            })
        return web.json_response(sessions)

    async def _handle_create_session(self, request: web.Request) -> web.Response:
        """POST /api/sessions — create a new conversation.

        Optional JSON body: ``{"pool": "pool_name"}`` to assign this
        conversation to a specific pool at creation time.
        """
        conv_id = _new_conversation_id()
        # Don't add to _conversations yet — only add when the first
        # message is sent (in _ws_send_message).  This prevents empty
        # conversations from appearing in the sidebar.

        pool_name: str | None = None
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw_pool = body.get("pool")
                if isinstance(raw_pool, str) and raw_pool:
                    pool_name = raw_pool
        except Exception:
            pass  # No JSON body or empty body — use defaults

        if pool_name is not None:
            self._conversation_pools[conv_id] = pool_name
            self._save_pool_mapping()

        response: dict[str, object] = {
            "conversation_id": conv_id,
            "pool": pool_name if pool_name is not None else _DEFAULT_AGENT_NAME,
        }
        return web.json_response(response)

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{conv_id}/messages — load transcript events.

        Query parameters:
            all=true  — merge events from ALL agents, sorted by timestamp
            agent=X   — load events for a specific agent (default: "main")
        """
        conv_id: str = request.match_info["conv_id"]
        load_all = request.query.get("all", "").lower() == "true"

        if load_all:
            events: list[dict[str, object]] = [
                e.to_dict() for e in self._store.load_all(conv_id)
            ]
        else:
            agent_name = request.query.get("agent", _DEFAULT_AGENT_NAME)
            events = [e.to_dict() for e in self._store.load(conv_id, agent_name)]
        return web.json_response(events)

    async def _handle_delete_session(self, request: web.Request) -> web.Response:
        """DELETE /api/sessions/{conv_id} — delete a conversation."""
        conv_id: str = request.match_info["conv_id"]
        self._store.delete_conversation(conv_id)
        self._conversations.discard(conv_id)
        if self._conversation_pools.pop(conv_id, None) is not None:
            self._save_pool_mapping()
        return web.json_response({"deleted": conv_id})

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
        """WebSocket endpoint — handles attach, send_message, new/delete conversation.

        Messages are JSON with an ``action`` field whose value is one of
        :class:`WebSocketAction`.
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        current_session_id: str | None = None
        delta_task: asyncio.Task[None] | None = None

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    current_session_id, delta_task = await self._dispatch_ws_message(
                        ws, msg.data, current_session_id, delta_task
                    )
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", ws.exception())
        except Exception:
            logger.exception("WebSocket handler error")
        finally:
            if delta_task is not None and not delta_task.done():
                delta_task.cancel()
                try:
                    await delta_task
                except asyncio.CancelledError:
                    pass

            if current_session_id is not None:
                self._input.unregister_connection(current_session_id)

        return ws

    async def _dispatch_ws_message(
        self,
        ws: web.WebSocketResponse,
        raw: str,
        current_session_id: str | None,
        delta_task: asyncio.Task[None] | None,
    ) -> tuple[str | None, asyncio.Task[None] | None]:
        """Parse and dispatch a single WebSocket text message.

        Returns the (possibly updated) *current_session_id* and *delta_task*.
        """
        try:
            data: dict[str, object] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in WebSocket message")
            return current_session_id, delta_task

        action = str(data.get("action", ""))

        if action == WebSocketAction.ATTACH:
            return await self._ws_attach(ws, data, current_session_id, delta_task)

        if action == WebSocketAction.SEND_MESSAGE:
            return await self._ws_send_message(ws, data, current_session_id, delta_task)

        if action == WebSocketAction.NEW_CONVERSATION:
            return self._ws_new_conversation(ws, current_session_id, delta_task)

        if action == WebSocketAction.DELETE_CONVERSATION:
            return self._ws_delete_conversation(ws, data, current_session_id, delta_task)

        logger.warning("Unknown WebSocket action: %s", action)
        return current_session_id, delta_task

    # -- action handlers -----------------------------------------------------

    async def _ws_attach(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        current_session_id: str | None,
        delta_task: asyncio.Task[None] | None,
    ) -> tuple[str | None, asyncio.Task[None] | None]:
        conv_id = str(data.get("conversation_id", ""))
        if not conv_id:
            asyncio.create_task(
                ws.send_json({"event": WebUIEventType.ERROR.value, "message": "conversation_id required"})
            )
            return current_session_id, delta_task

        # Unregister any previous session.
        if current_session_id is not None:
            self._input.unregister_connection(current_session_id)
        if delta_task is not None and not delta_task.done():
            delta_task.cancel()
            try:
                await delta_task
            except asyncio.CancelledError:
                pass

        session_id = _make_session_id(conv_id)
        self._input.register_connection(session_id, ws)
        self._conversations.add(conv_id)

        # Restore pool routing for this conversation (T3: pool per conversation).
        pool_name = self._conversation_pools.get(conv_id)
        if pool_name and self._pool_switch_callback is not None:
            self._pool_switch_callback(conv_id, pool_name)

        # Proactively register ALL pool agent sessions so deltas from any
        # pool's agent are forwarded to this WebSocket client.
        for agent_name in self._pool_agent_names:
            if agent_name == _DEFAULT_AGENT_NAME:
                continue  # already registered above
            pool_sid = _make_session_id(conv_id, agent_name)
            if self._input.get_delta_queue(pool_sid) is None:
                self._input.register_connection(pool_sid, ws)
                asyncio.create_task(self._forward_deltas(pool_sid, ws))

        # Also register subagent sessions found in transcript (for history).
        for agent_name in self._store.list_agents(conv_id):
            if agent_name in self._pool_agent_names:
                continue  # already handled above
            sub_sid = _make_session_id(conv_id, agent_name)
            if self._input.get_delta_queue(sub_sid) is None:
                self._input.register_connection(sub_sid, ws)
                asyncio.create_task(self._forward_deltas(sub_sid, ws))

        new_delta_task = asyncio.create_task(self._forward_deltas(session_id, ws))

        asyncio.create_task(
            ws.send_json({"event": WebUIEventType.ATTACHED.value, "conversation_id": conv_id})
        )
        return session_id, new_delta_task

    async def _ws_send_message(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        current_session_id: str | None,
        delta_task: asyncio.Task[None] | None,
    ) -> tuple[str | None, asyncio.Task[None] | None]:
        conv_id = str(data.get("conversation_id", ""))
        content = str(data.get("content", ""))
        # Pool is read from the stored conversation→pool mapping (T3).
        pool_name: str | None = self._conversation_pools.get(conv_id)
        if not conv_id or not content:
            return current_session_id, delta_task

        # Set pool routing via callback (if provided) so the PoolRouter
        # dispatches to the correct pool.
        if pool_name and self._pool_switch_callback is not None:
            self._pool_switch_callback(conv_id, pool_name)

        session_id = _make_session_id(conv_id)
        agent_name: str = pool_name or _DEFAULT_AGENT_NAME
        event = UserMessageEvent(
            conversation_id=conv_id,
            agent_name=agent_name,
            content=content,
        )
        self._store.append(conv_id, agent_name, event)
        self._conversations.add(conv_id)

        # Echo the user message back to the WS client so the frontend
        # can display it immediately.
        await ws.send_json(event.to_dict())

        # Enqueue with just conversation_id — PoolRouter adds agent_name
        # via DefaultSessionIdStrategy, matching our delta queue key.
        self._input.enqueue_user_message(conv_id, content)
        return current_session_id, delta_task

    def _ws_new_conversation(
        self,
        ws: web.WebSocketResponse,
        current_session_id: str | None,
        delta_task: asyncio.Task[None] | None,
    ) -> tuple[str | None, asyncio.Task[None] | None]:
        conv_id = _new_conversation_id()
        self._conversations.add(conv_id)
        asyncio.create_task(
            ws.send_json({
                "event": WebUIEventType.CONVERSATION_READY.value,
                "conversation_id": conv_id,
            })
        )
        return current_session_id, delta_task

    def _ws_delete_conversation(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        current_session_id: str | None,
        delta_task: asyncio.Task[None] | None,
    ) -> tuple[str | None, asyncio.Task[None] | None]:
        conv_id = str(data.get("conversation_id", ""))
        if conv_id:
            self._store.delete_conversation(conv_id)
            self._conversations.discard(conv_id)
            if self._conversation_pools.pop(conv_id, None) is not None:
                self._save_pool_mapping()
            asyncio.create_task(
                ws.send_json({
                    "event": WebUIEventType.CONVERSATION_DELETED.value,
                    "conversation_id": conv_id,
                })
            )
        return current_session_id, delta_task

    # ------------------------------------------------------------------
    # Delta forwarding
    # ------------------------------------------------------------------

    async def _forward_deltas(
        self, session_id: str, ws: web.WebSocketResponse
    ) -> None:
        """Background task: read deltas from the per-session queue and send them."""
        try:
            q = self._input.get_delta_queue(session_id)
            if q is None:
                return
            while True:
                delta: str = await q.get()
                await ws.send_str(delta)
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Delta forwarding error for session %s", session_id)
