"""S4: tag the conversation with its originating channel.

Uses the same prefix derivation as S5 (``conversation_session_prefix``) so
the key matches every downstream lookup. The channel-tracking store is a
deployment concern: the stage records ``(session_prefix, channel)`` through
the injected recorder callback (the bot passes
``bot.adapters.channels.set_conv_channel``).

IMPORTANT: Do NOT use the raw ``envelope.external_id`` directly.
IM adapters (QQ, etc.) pass the raw user/group ID as the external_id,
but S2/S5 encode it into a deterministic prefix.  If S4 stores the raw
ID but downstream lookups use the encoded prefix (which they must —
session_ids carry the encoded form), control-command responses (/pwd, /cd,
/exit) will silently route to the wrong channel (WebSocket fallback instead
of the IM adapter).  This encoding mismatch affected ALL IM channels.
"""

from __future__ import annotations

from collections.abc import Callable

from modex_agent.pipeline.input.context import InputContext
from modex_agent.pipeline.input.envelope import UserInputEnvelope
from modex_agent.pipeline.input.stage import Continue, InputStage, StageResult
from modex_agent.pipeline.input.stages.resolve_pool import conversation_session_prefix

#: Deployment-injected channel recorder: ``(session_prefix, channel_name)``.
ChannelRecorder = Callable[[str, str], None]


class SetChannelStage(InputStage):
    """Tag the conversation with its originating channel via the injected
    recorder (the channel-tracking store is deployment-owned)."""

    def __init__(self, recorder: ChannelRecorder) -> None:
        self._recorder = recorder

    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        session_prefix = conversation_session_prefix(envelope, ctx)
        self._recorder(session_prefix, envelope.channel)
        return Continue(value=envelope)
