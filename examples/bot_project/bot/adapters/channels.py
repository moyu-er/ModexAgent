"""Multi-channel spine — channel tracking + the channel-router output adapter.

Channel registration goes through the bot's ``BotChannelsPlugin``
(``bot_plugins/bot_channels.py``) via the framework
``PluginRegistrationContext.register_channel_adapter`` face; the
``@register`` import-side-effect registry is gone.

Channel tracking: ``set_conv_channel / get_conv_channel`` records which
channel originated each conversation.  Emitters use this to avoid
cross-talk — a QQ emitter only sends to QQ users, never to WebUI or Slack.

WebUI (websocket) is the universal observer: its emitter records ALL
conversations to the transcript store so the frontend can view history
from any channel.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import KindGate
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.models import OutputMessage
from modex_agent.plugins.loader import ChannelBuildContext

if TYPE_CHECKING:
    from bot.webui.transcript_store import TranscriptStore


# ── Channel tracking (session_id → channel_name) ────────────────────

_conversation_channels: dict[str, str] = {}


def get_conv_channel(conv_id: str) -> str:
    """Return the channel that originated *conv_id*, defaulting to websocket."""
    return _conversation_channels.get(conv_id, "websocket")


def set_conv_channel(conv_id: str, channel: str) -> None:
    """Record the channel that originated *conv_id*."""
    _conversation_channels[conv_id] = channel


# ── IM-channel sink gate ──────────────────────────────────────────────────


IM_CHANNEL_KINDS: frozenset[str] = frozenset(
    {
        "text",
        "tool_call",
        "tool_result",
        "turn_finished",
        "turn_errored",
        "iteration_finished",
    }
)
"""The delivered-kind set every IM channel sink (QQ, Telegram) gates on."""


def im_channel_gate(
    enabled: AbstractSet[str] | None = None,
    disabled: AbstractSet[str] | None = None,
) -> KindGate:
    """Build the IM-channel sink gate — the single owner of the kind set.

    Kind literals are the core ``TurnEvent`` kinds (old enum-event-name
    migration: model_output→text, tool_call_start→tool_call,
    tool_call_end→tool_result, final_output→turn_finished, error→
    turn_errored). ``iteration_finished`` was not in the old set, but it
    is the buffering policy's (SEGMENT) flush boundary, so it stays
    enabled.
    """
    return KindGate(
        enabled_kinds=frozenset(enabled) if enabled is not None else IM_CHANNEL_KINDS,
        disabled_kinds=frozenset(disabled) if disabled is not None else frozenset(),
    )


# ── Build context passed to each adapter's build() ──────────────────────


@dataclass(frozen=True)
class AdapterBuildContext(ChannelBuildContext):
    """The bot's channel-build context: the framework face plus the
    deployment's project root and transcript store."""

    project_dir: Path
    """Project root (examples/bot_project/)."""

    transcript_store: TranscriptStore
    """The workspace-scoped transcript store for persisting turns."""


# ── Channel-aware output router ─────────────────────────────────────────


def _session_to_session_id(session_id: str) -> str:
    """Extract the session id prefix from a session identifier.

    Handles both canonical ``{prefix}.{agent}`` IDs and raw prefixes.
    """
    try:
        session = SessionInfo.from_str(session_id)
    except Exception:
        return session_id
    return session.session_id_prefix


class ChannelRouterOutputAdapter(OutputAdapter):
    """Routes output to the channel-specific adapter that owns the conversation.

    In multi-channel services (QQ + WebUI), control notices, pool switch
    notifications, and pipeline command responses must be delivered back to
    the channel the user is talking on.  This adapter uses
    :func:`get_conv_channel` to look up the originating channel and delegates
    to the matching per-channel output adapter.
    """

    def __init__(self, adapters: dict[str, OutputAdapter]) -> None:
        if not adapters:
            raise ValueError("ChannelRouterOutputAdapter requires at least one adapter")
        self._adapters = dict(adapters)
        self._fallback = self._adapters.get(
            "websocket", next(iter(self._adapters.values()))
        )

    @property
    def name(self) -> str:
        return "channel_router"

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.PSEUDO

    def _resolve(self, session_id: str) -> OutputAdapter:
        conv_id = _session_to_session_id(session_id)
        channel = get_conv_channel(conv_id)
        adapter = self._adapters.get(channel)
        if adapter is None:
            adapter = self._fallback
        return adapter

    async def send(self, message: OutputMessage, session_id: str) -> None:
        # Transient user notices (message_type=notice) are fanned out to the
        # originating channel AND the WebUI (universal observer), so an
        # IM-originated notice is visible in both IM and the browser. WebUI-
        # originated turns already receive it via the originating leg, so the
        # extra WebUI send is skipped (no duplicate bubble). The WebUI leg is
        # also skipped when no websocket adapter is registered (pure-IM deploy).
        await self._resolve(session_id).send(message, session_id)
        if message.message_type == "notice":
            conv_id = _session_to_session_id(session_id)
            if get_conv_channel(conv_id) != "websocket":
                webui = self._adapters.get("websocket")
                if webui is not None:
                    await webui.send(message, session_id)

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict | None = None
    ) -> None:
        await self._resolve(session_id).send_delta(delta, session_id, metadata)

    async def flush_deltas(self, session_id: str) -> None:
        await self._resolve(session_id).flush_deltas(session_id)
