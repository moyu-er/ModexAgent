"""Telegram adapter registration.

Reads the ``telegram`` section from ``raw_config`` (populated from
``config/im.yml``). When Telegram is disabled or the bot token is empty,
the build is skipped gracefully (logs a notice and returns ``None``) —
mirrors :mod:`bot.adapters.register_qq`.

When enabled, this factory:

* builds a ``python-telegram-bot`` (PTB) ``Application`` for long-polling,
* wires the inbound ``MessageHandler`` so text updates flow into
  :class:`bot.adapters.telegram.TelegramInputAdapter`,
* injects PTB start/stop coroutines into the input adapter via
  :meth:`set_lifecycle_hooks` (keeping the adapter PTB-free + unit-testable),
* constructs a :class:`TelegramOutputAdapter` bound to the PTB bot, and
* returns a channel-filtered emitter factory so Telegram-originated
  conversations receive output while WebUI/other-channel convs do not
  (no cross-talk).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bot.adapters.channels import (
    AdapterBuildContext,
    get_conv_channel,
    register,
    set_conv_channel,
)
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.emitter import AgentResult, EmitterConfig, StreamingAwareEmitter
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.pipeline.adapters import OutputAdapter

if TYPE_CHECKING:
    from bot.adapters.telegram import TelegramInputAdapter, TelegramOutputAdapter

logger = logging.getLogger(__name__)


def _telegram_enabled(ctx: AdapterBuildContext) -> bool:
    """Return True iff the Telegram section is enabled AND has a token.

    ``ctx.raw_config`` is a loose runtime-config boundary that is still
    untyped at this point; ``dict[str, object]`` is acceptable here.
    """
    tg_cfg: dict[str, object] = ctx.raw_config.get("telegram") or {}
    # telegram_enabled already checks both ``enabled`` and ``token``.
    from bot.adapters.telegram import telegram_enabled

    return telegram_enabled(tg_cfg)


@register("telegram", enabled=True)
def build_telegram(
    ctx: AdapterBuildContext,
) -> (
    tuple[
        TelegramInputAdapter,
        TelegramOutputAdapter,
        Callable[[str], StreamingAwareEmitter[ReActEvent]],
    ]
    | None
):
    """Build Telegram channel adapters + emitter factory.

    Returns ``(input, output, emitter_factory)`` when Telegram is configured,
    or ``None`` when disabled/skipped.
    """
    if not _telegram_enabled(ctx):
        logger.info("Telegram adapter: not configured, skipping")
        return None

    # Lazy imports: keeps module import cheap when Telegram is unused, and
    # contains the PTB SDK dependency to this factory only.
    from telegram.ext import Application, MessageHandler, filters

    from bot.adapters.telegram import (
        TelegramInputAdapter,
        TelegramOutputAdapter,
    )

    # raw_config section is a loose runtime-config boundary (dict[str, object]).
    tg_cfg: dict[str, object] = ctx.raw_config.get("telegram") or {}
    token = str(tg_cfg["token"])  # validated non-empty by _telegram_enabled
    proxy_raw = tg_cfg.get("proxy")
    proxy = str(proxy_raw) if proxy_raw else None
    allow_from_raw = tg_cfg.get("allow_from", ["*"])
    # isinstance narrowing at the raw-config boundary: the YAML section is
    # typed dict[str, object], so the allow_from value must be narrowed to a
    # sequence before element-wise str coercion. This is the config/SDK
    # compatibility boundary the type-safety rules carve out.
    allow_from: list[str] = (
        [str(s) for s in allow_from_raw]
        if isinstance(allow_from_raw, list | tuple)
        else ["*"]
    )

    builder = Application.builder().token(token)
    if proxy:
        # PTB HTTPXRequest carries the proxy; applied to both the outbound
        # request and the getUpdates (long-poll) request.
        from telegram.request import HTTPXRequest

        builder = builder.request(HTTPXRequest(proxy=proxy)).get_updates_request(
            HTTPXRequest(proxy=proxy)
        )
    application = builder.build()

    inp = TelegramInputAdapter(token=token, allow_from=allow_from, proxy=proxy)

    async def _on_message(  # noqa: ANN202  # PTB SDK boundary: untyped callback contract
        update: Any,  # noqa: ANN401  # PTB Update object — external-SDK boundary
        context: Any,  # noqa: ANN401  # PTB Context — external-SDK boundary
    ) -> None:
        # PTB Update/Message objects are an external-SDK boundary: attribute
        # access (effective_message / effective_user / chat_id) is the
        # documented PTB API surface — getattr on .text guards messages that
        # carry no text payload (stickers, media, etc.) without crashing.
        del context  # PTB passes a Context we don't use here
        msg = getattr(update, "effective_message", None)
        if msg is None:
            return
        text = getattr(msg, "text", None)
        if not text:
            return
        chat_id = str(msg.chat_id)
        user = getattr(update, "effective_user", None)
        sender_id = str(user.id) if user is not None else chat_id
        # Record this conversation as Telegram-originated so the channel-filtered
        # emitter routes replies here. COUPLING CONTRACT: enqueue_update() builds
        # the session id "{chat_id}.main", and the emitter resolves the conv id
        # via session_id_prefix_of(sid) — which yields ``chat_id``. So the map
        # key here MUST be the bare chat_id (the session prefix), or the filter
        # in _ChannelFilteredTelegramEmitter would silently drop all output.
        set_conv_channel(chat_id, "telegram")
        inp.enqueue_update(chat_id=chat_id, text=text, sender_id=sender_id)

    application.add_handler(MessageHandler(filters.TEXT, _on_message))

    async def _start() -> None:
        await application.initialize()
        await application.start()
        # PTB Application.updater is Updater | None until initialize() wires it;
        # guard None the same way _stop does.
        if application.updater is not None:
            await application.updater.start_polling(allowed_updates=None)

    async def _stop() -> None:
        # PTB Application always exposes an ``.updater`` attribute once built;
        # guard None for robustness but avoid getattr (direct attribute access
        # is the documented PTB API).
        try:
            if application.updater is not None:
                await application.updater.stop()
            await application.stop()
            await application.shutdown()
        except Exception:  # noqa: BLE001  # external-SDK boundary: never raise into caller
            logger.exception("Telegram Application shutdown failed")

    inp.set_lifecycle_hooks(on_start=_start, on_stop=_stop)

    out = TelegramOutputAdapter(bot=application.bot)

    def emitter_factory(session_id: str) -> StreamingAwareEmitter[ReActEvent]:
        """Create a channel-filtered Telegram emitter for *session_id*.

        Mirrors the QQ register's ``_ChannelFilteredQQEmitter``: the emitter
        silently drops all output when the conversation did not originate on
        Telegram (no cross-talk to WebUI/QQ users).
        """

        class _ChannelFilteredTelegramEmitter(StreamingAwareEmitter[ReActEvent]):
            """Telegram emitter that only sends for Telegram-originated convs."""

            def __init__(
                self,
                output_adapter: OutputAdapter,
                sid: str,
                config: EmitterConfig | None = None,
            ) -> None:
                super().__init__(output_adapter, sid, config)
                # session_id format: {conv_id}.{agent}[.{invocation_id}]
                self._conv_id = session_id_prefix_of(sid)

            async def emit_delta(self, delta: str) -> None:
                if get_conv_channel(self._conv_id) != "telegram":
                    return
                await super().emit_delta(delta)

            async def emit_content(self, full_content: str) -> None:
                if get_conv_channel(self._conv_id) != "telegram":
                    return
                await super().emit_content(full_content)

            async def emit_stream_end(self, resuming: bool = False) -> None:
                if get_conv_channel(self._conv_id) != "telegram":
                    return
                await super().emit_stream_end(resuming)

            async def emit_complete(self, result: AgentResult) -> None:
                if get_conv_channel(self._conv_id) != "telegram":
                    return
                await super().emit_complete(result)

            async def emit_error(self, error: str) -> None:
                if get_conv_channel(self._conv_id) != "telegram":
                    return
                await super().emit_error(error)

        return _ChannelFilteredTelegramEmitter(
            output_adapter=out,
            sid=session_id,
            config=EmitterConfig(
                enabled_events={
                    "model_output",
                    "tool_call_start",
                    "tool_call_end",
                    "final_output",
                    "error",
                }
            ),
        )

    logger.info("Telegram adapter: built (proxy=%s)", bool(proxy))
    return inp, out, emitter_factory
