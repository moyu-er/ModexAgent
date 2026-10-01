"""Bot channel-adapter registration (W4b unification).

Registers the QQ / Telegram / WebSocket adapter factories through the
framework ``PluginRegistrationContext.register_channel_adapter`` face —
the import-side-effect ``@register``/``ADAPTERS`` parallel registry is
gone. ``WebUIService`` resolves adapters by name from the
``ChannelAdapterRegistry``.

The build functions stay in ``bot/adapters/register_*.py``; the typed
closure here narrows the framework's generic ``ChannelBuildContext`` to
the bot's ``AdapterBuildContext`` (the one extension boundary where the
deployment context shape is checked).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import BaseModel

from modex_agent.plugins.loader import (
    ChannelAdapterFactory,
    ChannelBuildContext,
    ChannelBuildResult,
    Plugin,
    PluginRegistrationContext,
)

if TYPE_CHECKING:
    from bot.adapters.channels import AdapterBuildContext

__all__ = ["BotChannelsPlugin"]


class BotChannelsConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


def _typed(
    build: Callable[[AdapterBuildContext], ChannelBuildResult | None],
) -> ChannelAdapterFactory:
    """Adapt a bot build function (over the bot context subclass) to the
    framework factory signature. The ``isinstance`` narrowing is the
    deployment-context extension boundary."""

    def factory(ctx: ChannelBuildContext) -> ChannelBuildResult | None:
        from bot.adapters.channels import AdapterBuildContext

        assert isinstance(ctx, AdapterBuildContext), (
            "bot channel factories require the bot AdapterBuildContext"
        )
        return build(ctx)

    return factory


class BotChannelsPlugin(Plugin):
    """Registers the shipped channel adapters (qq, telegram, websocket)."""

    config_model = BotChannelsConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        from bot.adapters.register_qq import build_qq
        from bot.adapters.register_telegram import build_telegram
        from bot.adapters.register_websocket import build_websocket

        # Registration order == build order at service boot: qq, telegram,
        # websocket (the historical discovery order).
        ctx.register_channel_adapter("qq", _typed(build_qq))
        ctx.register_channel_adapter("telegram", _typed(build_telegram))
        ctx.register_channel_adapter("websocket", _typed(build_websocket))
