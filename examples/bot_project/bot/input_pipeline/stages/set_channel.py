"""S4: tag the conversation with its originating channel.

Uses the same prefix derivation as S5 (``conversation_session_prefix``) so
the key matches every downstream lookup: ``get_conv_channel`` in
``ChannelRouterOutputAdapter``, the QQ emitter, and pool routing.

IMPORTANT: Do NOT use the raw ``envelope.external_id`` directly.
IM adapters (QQ, etc.) pass the raw user/group ID as the external_id,
but S2/S5 encode it into a deterministic prefix.  If S4 stores the raw
ID but downstream lookups use the encoded prefix (which they must —
session_ids carry the encoded form), control-command responses (/pwd, /cd,
/exit) will silently route to the wrong channel (WebSocket fallback instead
of the IM adapter).  This encoding mismatch affected ALL IM channels.
"""

from __future__ import annotations

from bot.adapters.channels import set_conv_channel
from bot.input_pipeline.stages.resolve_pool import conversation_session_prefix
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult

from bot.input_pipeline.context import BotInputContext


class SetChannelStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        session_prefix = conversation_session_prefix(envelope, ctx)
        set_conv_channel(session_prefix, envelope.channel)
        return Continue(value=envelope)
