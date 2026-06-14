"""S4: tag the conversation with its originating channel."""

from __future__ import annotations

from bot.adapters.channels import set_conv_channel
from framework.input_pipeline.context import InputContext
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult


class SetChannelStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        set_conv_channel(envelope.conversation_id, envelope.channel)
        return Continue(value=envelope)
