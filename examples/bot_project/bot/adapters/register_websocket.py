"""WebSocket adapter registration for WebUI channel.

This adapter is always enabled — it provides the WebUI's browser-facing
WebSocket transport.  The ``WebUIServer`` accesses the input adapter
directly for delta queue management and user-message enqueuing.
"""

from __future__ import annotations

from collections.abc import Callable

from bot.adapters.channels import AdapterBuildContext, register
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import SessionMeta
from bot.webui.transcript_store import TranscriptStore
from framework.core.emitter import EmitterConfig


def _default_meta_resolver(session_id: str) -> SessionMeta:
    """Default resolver: no business routing context known."""
    return SessionMeta()


# Module-level references so WebUIServer can access the WebSocket adapter
# without circular imports.
_ws_input: WebSocketInputAdapter | None = None
_ws_output: WebSocketOutputAdapter | None = None
# Lazy resolver for per-session business routing context (pool,
# parent_session_id).  Populated by WebUIService after pool init; read at
# emit time so it always reflects the latest pool map / parent registry.
_session_meta_resolver: Callable[[str], SessionMeta] = _default_meta_resolver


def get_ws_input() -> WebSocketInputAdapter:
    """Return the shared WebSocket input adapter (created during build)."""
    assert _ws_input is not None, "WebSocket adapter not built yet"
    return _ws_input


def get_ws_output() -> WebSocketOutputAdapter:
    """Return the shared WebSocket output adapter."""
    assert _ws_output is not None, "WebSocket adapter not built yet"
    return _ws_output


def set_session_meta_resolver(resolver: Callable[[str], SessionMeta]) -> None:
    """Inject the per-session business routing resolver (pool, parent).

    Called by WebUIService.start() once the agent→pool map and the parent
    registry are ready.  The resolver is read lazily at emit time.
    """
    global _session_meta_resolver
    _session_meta_resolver = resolver


@register("websocket", enabled=True)
def build_websocket(ctx: AdapterBuildContext):
    """Build WebSocket channel adapters + emitter."""
    global _ws_input, _ws_output

    _ws_input = WebSocketInputAdapter()
    _ws_output = WebSocketOutputAdapter(_ws_input)

    store = ctx.transcript_store
    assert isinstance(store, TranscriptStore)

    def emitter_factory(session_id: str) -> WebBotEmitter:
        return WebBotEmitter(
            output_adapter=_ws_output,
            session_id=session_id,
            config=EmitterConfig(),
            transcript_store=store,
            session_meta_resolver=_resolve_meta_for(session_id),
        )

    return _ws_input, _ws_output, emitter_factory


def _resolve_meta_for(session_id: str) -> Callable[[], SessionMeta]:
    """Bind a resolver that captures *session_id* and reads the global resolver."""

    def _resolve() -> SessionMeta:
        return _session_meta_resolver(session_id)

    return _resolve
