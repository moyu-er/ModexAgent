"""Adapter registry — multi-channel IM support.

Each IM adapter declares itself here with a hardcoded ``enabled`` flag.
To add a new IM (Slack, Discord, Telegram, etc.), add one ``AdapterSpec``
to the ``ADAPTERS`` list — no other code changes needed.

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

if TYPE_CHECKING:
    from framework.core.emitter import ContentEmitter
    from framework.pipeline.adapters import InputAdapter, OutputAdapter


# ── Channel tracking (conversation_id → channel_name) ────────────────────

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
