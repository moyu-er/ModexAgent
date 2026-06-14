"""S8: build the final InputMessage and enqueue it via ctx.enqueue_message.

Channel-agnostic: the physical queue (QQ _message_queue / WS queue) is injected
through the context's enqueue_message callback, so this stage never touches a
WS-specific method.
"""

from __future__ import annotations

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from framework.core.types import InputMessage
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult


class EnqueueStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        llm_content = envelope.metadata.get(RoutingMeta.SKILL_XML) or envelope.content
        attachments = [a.local_path for a in envelope.attachments if a.local_path]
        msg = InputMessage(
            content=llm_content,
            session_id=envelope.conversation_id,
            channel=envelope.channel,
            source=envelope.channel,  # PoolRouter uses msg.source for AgentAddress name
            chat_id=envelope.metadata.get("chat_id", ""),  # broker header; never drop to default
            metadata=envelope.metadata,
            attachments=attachments,
        )
        ctx.enqueue_message(msg)
        return Continue(value=envelope)
