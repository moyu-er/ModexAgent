"""WebSocket adapter registration for WebUI channel.

This adapter is always enabled — it provides the WebUI's browser-facing
WebSocket transport.  The ``WebUIServer`` accesses the input adapter
directly for delta queue management and user-message enqueuing.
"""

from __future__ import annotations

from bot.adapters.channels import AdapterBuildContext, register
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import WebBotEmitter
from bot.webui.transcript_store import TranscriptStore
from framework.core.emitter import EmitterConfig

# Module-level references so WebUIServer can access the WebSocket adapter
# without circular imports.
_ws_input: WebSocketInputAdapter | None = None
_ws_output: WebSocketOutputAdapter | None = None


def get_ws_input() -> WebSocketInputAdapter:
    """Return the shared WebSocket input adapter (created during build)."""
    assert _ws_input is not None, "WebSocket adapter not built yet"
    return _ws_input


def get_ws_output() -> WebSocketOutputAdapter:
    """Return the shared WebSocket output adapter."""
    assert _ws_output is not None, "WebSocket adapter not built yet"
    return _ws_output


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
        )

    return _ws_input, _ws_output, emitter_factory
