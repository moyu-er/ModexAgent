"""Adapter registry — multi-channel IM support.

Each IM adapter declares itself here with a hardcoded ``enabled`` flag.
To add a new IM (Slack, Discord, Telegram, etc.), add a
``register_<name>.py`` module under ``bot/adapters/`` and use the
``@register`` decorator.  ``WebUIService`` auto-discovers all
``register_*.py`` modules at startup, so no other code changes are needed.

Channel tracking: ``set_conv_channel / get_conv_channel`` records which
channel originated each conversation.  Emitters use this to avoid
cross-talk — a QQ emitter only sends to QQ users, never to WebUI or Slack.

WebUI (websocket) is the universal observer: its emitter records ALL
conversations to the transcript store so the frontend can view history
from any channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from framework.adapters.platform import StreamingMode
from framework.core.types import OutputMessage
from framework.core.session_id import SessionInfo
from framework.pipeline.adapters import OutputAdapter

if TYPE_CHECKING:
    from framework.core.emitter import ContentEmitter
    from framework.pipeline.adapters import InputAdapter


# ── Channel tracking (session_id → channel_name) ────────────────────

_conversation_channels: dict[str, str] = {}


def get_conv_channel(conv_id: str) -> str:
    """Return the channel that originated *conv_id*, defaulting to websocket."""
    return _conversation_channels.get(conv_id, "websocket")


def set_conv_channel(conv_id: str, channel: str) -> None:
    """Record the channel that originated *conv_id*."""
    _conversation_channels[conv_id] = channel


# ── Build context passed to each adapter's build() ──────────────────────


@dataclass
class AdapterBuildContext:
    """Context available to each adapter's ``build()`` factory."""

    config_dir: Path
    """Path to the config/ directory."""

    project_dir: Path
    """Project root (examples/bot_project/)."""

    raw_config: dict
    """Raw bot_config.yml as a dict (for QQ app_id/secret etc.)."""

    transcript_store: object
    """JSONLTranscriptStore for persisting turns."""


# ── Adapter spec ────────────────────────────────────────────────────────


@dataclass
class AdapterSpec:
    """Describes one IM adapter.

    Attributes:
        name: Unique channel name (e.g. ``"qq"``, ``"websocket"``).
        enabled: Hardcoded toggle — set to ``False`` to skip this adapter.
        build: Factory: ``(AdapterBuildContext) → (InputAdapter, OutputAdapter, ContentEmitter)``.
    """

    name: str
    enabled: bool
    build: Callable[
        [AdapterBuildContext],
        tuple["InputAdapter", "OutputAdapter", "ContentEmitter"],
    ]


# ── Registry ────────────────────────────────────────────────────────────


ADAPTERS: list[AdapterSpec] = []
"""All registered adapters.  Append to this list to add a new IM."""


def register(name: str, *, enabled: bool = True):
    """Decorator that registers an adapter build function.

    Usage::

        @register("qq", enabled=True)
        def build_qq(ctx: AdapterBuildContext):
            ...
            return input_adapter, output_adapter, emitter
    """

    def _decorator(
        fn: Callable[
            [AdapterBuildContext],
            tuple["InputAdapter", "OutputAdapter", "ContentEmitter"],
        ],
    ):
        ADAPTERS.append(AdapterSpec(name=name, enabled=enabled, build=fn))
        return fn

    return _decorator


# ── Channel-aware output router ─────────────────────────────────────────


def _session_to_session_id(session_id: str) -> str:
    """Extract the session id prefix from a session identifier.

    Handles both canonical ``{prefix}.{agent}`` IDs and raw prefixes.
    """
    try:
        session = SessionInfo.from_str(session_id, default_agent_name="main")
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
        await self._resolve(session_id).send(message, session_id)

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict | None = None
    ) -> None:
        await self._resolve(session_id).send_delta(delta, session_id, metadata)

    async def flush_deltas(self, session_id: str) -> None:
        await self._resolve(session_id).flush_deltas(session_id)
