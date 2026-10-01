"""QQ adapter registration.

Enabled by default — reads ``qq`` section from ``bot_config.yml``.
If the config section is missing or ``app_id`` is empty, the adapter
build is skipped gracefully (logs a warning and returns).
"""

from __future__ import annotations

from bot.adapters.channels import AdapterBuildContext, get_conv_channel, im_channel_gate
from modex_agent.core.emitter import TurnBinding, TurnEventSink
from modex_agent.core.session_id import session_id_prefix_of
from modex_agent.core.turn_events import TurnEvent


def _qq_enabled(ctx: AdapterBuildContext) -> bool:
    """Check whether QQ is configured."""
    qq_cfg = ctx.raw_config.get("qq", {})
    if not qq_cfg:
        return False
    return not (not qq_cfg.get("app_id") or not qq_cfg.get("secret"))


def build_qq(ctx: AdapterBuildContext):
    """Build QQ channel adapters + turn-event sink factory."""
    import logging

    logger = logging.getLogger(__name__)

    if not _qq_enabled(ctx):
        logger.info("QQ adapter: not configured, skipping")
        return None  # type: ignore[return-value]

    from bot.adapters.qq import (
        QQBotEmitter,
        QQInputAdapter,
        QQOutputAdapter,
    )
    qq_cfg: dict = ctx.raw_config.get("qq", {})

    qq_input = QQInputAdapter(
        app_id=qq_cfg["app_id"],
        secret=qq_cfg["secret"],
        allow_from=qq_cfg.get("allow_from", ["*"]),
        project_dir=ctx.project_dir,
    )
    qq_output_raw = QQOutputAdapter(qq_input)
    qq_output = qq_output_raw

    _raw_output = qq_output_raw
    _stripped_output = qq_output

    def emitter_factory(binding: TurnBinding) -> TurnEventSink:
        """Create a channel-filtered QQ sink for the bound turn."""

        class _ChannelFilteredQQEmitter(QQBotEmitter):
            """QQ sink that only sends for QQ-originated conversations.

            If the conversation was created via WebUI (or any other channel),
            this sink silently drops all output — no cross-talk.
            """

            def __init__(self, output_adapter, session_id, gate) -> None:
                super().__init__(output_adapter, session_id, gate)
                # session_id format: {conv_id}.{agent_name}
                self._conv_id = session_id_prefix_of(session_id)

            async def _dispatch(self, event: TurnEvent) -> None:
                if get_conv_channel(self._conv_id) != "qq":
                    return
                await super()._dispatch(event)

        return _ChannelFilteredQQEmitter(
            output_adapter=_raw_output,
            session_id=binding.session_id,
            gate=im_channel_gate(),
        )

    logger.info("QQ adapter: built (app_id=%s)", qq_cfg["app_id"])
    return qq_input, _stripped_output, emitter_factory
