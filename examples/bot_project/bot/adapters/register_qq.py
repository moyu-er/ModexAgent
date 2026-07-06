"""QQ adapter registration.

Enabled by default — reads ``qq`` section from ``bot_config.yml``.
If the config section is missing or ``app_id`` is empty, the adapter
build is skipped gracefully (logs a warning and returns).
"""

from __future__ import annotations

from pathlib import Path

from bot.adapters.channels import AdapterBuildContext, get_conv_channel, register
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import session_id_prefix_of


def _qq_enabled(ctx: AdapterBuildContext) -> bool:
    """Check whether QQ is configured."""
    qq_cfg = ctx.raw_config.get("qq", {})
    if not qq_cfg:
        return False
    if not qq_cfg.get("app_id") or not qq_cfg.get("secret"):
        return False
    return True


@register("qq", enabled=True)
def build_qq(ctx: AdapterBuildContext):
    """Build QQ channel adapters + emitter factory."""
    import logging

    logger = logging.getLogger(__name__)

    if not _qq_enabled(ctx):
        logger.info("QQ adapter: not configured, skipping")
        return None  # type: ignore[return-value]

    from bot.adapters.qq import (
        QQEmitterConfig,
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

    def emitter_factory(session_id: str):
        """Create a channel-filtered QQ emitter for *session_id*."""
        from bot.adapters.channels import get_conv_channel

        class _ChannelFilteredQQEmitter(QQBotEmitter):
            """QQ emitter that only sends for QQ-originated conversations.

            If the conversation was created via WebUI (or any other channel),
            this emitter silently drops all output — no cross-talk.
            """

            def __init__(self, output_adapter, session_id, config):
                super().__init__(output_adapter, session_id, config)
                # session_id format: {conv_id}.{agent_name}
                self._conv_id = session_id_prefix_of(session_id)

            async def emit_delta(self, delta: str) -> None:
                if get_conv_channel(self._conv_id) != "qq":
                    return
                await super().emit_delta(delta)

            async def emit_content(self, full_content: str) -> None:
                if get_conv_channel(self._conv_id) != "qq":
                    return
                await super().emit_content(full_content)

            async def emit_complete(self, result: AgentResult) -> None:
                if get_conv_channel(self._conv_id) != "qq":
                    return
                await super().emit_complete(result)

            async def emit_error(self, error: str) -> None:
                if get_conv_channel(self._conv_id) != "qq":
                    return
                await super().emit_error(error)

        return _ChannelFilteredQQEmitter(
            output_adapter=_raw_output,
            session_id=session_id,
            config=QQEmitterConfig.minimal(),
        )

    logger.info("QQ adapter: built (app_id=%s)", qq_cfg["app_id"])
    return qq_input, _stripped_output, emitter_factory
