"""S7: persist the raw user message to the transcript store."""

from __future__ import annotations

import logging

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.webui.events import UserMessageEvent
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult

logger = logging.getLogger(__name__)


class PersistUserMessageStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        content = envelope.content.strip()
        # Defense-in-depth: a valid skill invocation legitimately starts with
        # "/" and carries skill_xml (set by S6) — it must be persisted as the
        # raw text.  Only a "/" command WITHOUT skill_xml is a control command
        # that leaked past S2/S3/S6; skip persisting those.
        if content.startswith("/") and RoutingMeta.SKILL_XML not in envelope.metadata:
            logger.warning("Unexpected command reached persistence: %s", content)
            return Continue(value=envelope)

        full_sid = envelope.metadata[RoutingMeta.FULL_SESSION_ID]
        agent = envelope.metadata[RoutingMeta.RESOLVED_AGENT]
        event = UserMessageEvent(
            session_id=full_sid,
            agent_name=agent,
            content=envelope.content,
        )
        ctx.transcript_store.append(full_sid, event)
        return Continue(value=envelope)
