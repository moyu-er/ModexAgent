"""S4: tag the conversation with its originating channel.

Uses the same snowflake derivation as S5 (``conversation_snowflake``) so
the key matches every downstream lookup: ``get_conv_channel`` in
``ChannelRouterOutputAdapter``, the QQ emitter, and pool routing.

IMPORTANT: Do NOT use the raw ``envelope.conversation_id`` directly.
IM adapters (QQ, etc.) pass the raw user/group ID as the conversation_id,
but S2/S5 encode it into a deterministic snowflake.  If S4 stores the raw
ID but downstream lookups use the encoded snowflake (which they must —
session_ids carry the encoded form), control-command responses (/pwd, /cd,
/exit) will silently route to the wrong channel (WebSocket fallback instead
of the IM adapter).  This encoding mismatch affected ALL IM channels.
"""

from __future__ import annotations

from bot.adapters.channels import set_conv_channel
from bot.input_pipeline.stages.resolve_pool import conversation_snowflake
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult

from bot.input_pipeline.context import BotInputContext


class SetChannelStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        snowflake = conversation_snowflake(envelope, ctx)
        set_conv_channel(snowflake, envelope.channel)
        return Continue(value=envelope)
