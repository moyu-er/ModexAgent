"""WebSocket adapter registration for WebUI channel.

This adapter is always enabled — it provides the WebUI's browser-facing
WebSocket transport.  The ``WebUIServer`` accesses the input adapter
directly for delta queue management and user-message enqueuing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from bot.adapters.channels import AdapterBuildContext, register
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.webui.emitter import WebBotEmitter
from bot.webui.events import SessionMeta
from bot.webui.transcript_store import TranscriptStore
from modex_agent.core.events import EmitterConfig
from modex_agent.core.session_id import SessionIdFactory

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


def build_websocket_emitter(
    session_id: str,
    output_adapter: WebSocketOutputAdapter,
    transcript_store: TranscriptStore,
    *,
    pool: str | None = None,
    sessions_dir_provider: Callable[[], Path | None] | None = None,
    session_meta_resolver: Callable[[], SessionMeta] | None = None,
) -> WebBotEmitter:
    """Construct a WebBotEmitter wired to the shared WS adapter + store.

    Used both by the shared channel factory (no provider — ctxvar fallback)
    and by per-workspace factories (with a provider from the workspace's
    resolver cell).
    """
    return WebBotEmitter(
        output_adapter=output_adapter,
        session_id=session_id,
        config=EmitterConfig(),
        pool=pool,
        transcript_store=transcript_store,
        session_meta_resolver=session_meta_resolver,
        sessions_dir_provider=sessions_dir_provider,
    )


@register("websocket", enabled=True)
def build_websocket(ctx: AdapterBuildContext):
    """Build WebSocket channel adapters + emitter."""
    global _ws_input, _ws_output

    ws_input = WebSocketInputAdapter(session_factory=SessionIdFactory())
    ws_output = WebSocketOutputAdapter(ws_input)
    _ws_input = ws_input
    _ws_output = ws_output

    store = ctx.transcript_store
    assert isinstance(store, TranscriptStore)

    def emitter_factory(session_id: str, pool: str) -> WebBotEmitter:
        return build_websocket_emitter(
            session_id,
            output_adapter=ws_output,
            transcript_store=store,
            pool=pool,
            session_meta_resolver=_parent_meta_for(ws_input, session_id),
        )

    return ws_input, ws_output, emitter_factory


def _parent_meta_for(ws_input: WebSocketInputAdapter, session_id: str) -> Callable[[], SessionMeta]:
    """Bind a lazy parent-session resolver against the WS genealogy map.

    Parent lineage ONLY: pool ownership is fixed on each emitter by its
    factory's ``pool`` argument and is never resolved here. ``get_parent``
    is read at emit time so dispatch-time ``register_subagent`` entries
    appended after emitter construction are still reflected.
    """

    def _resolve() -> SessionMeta:
        return SessionMeta(parent_session_id=ws_input.get_parent(session_id))

    return _resolve
