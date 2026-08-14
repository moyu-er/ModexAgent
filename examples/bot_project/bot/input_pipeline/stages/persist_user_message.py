"""S7: persist the raw user message to the transcript store."""

from __future__ import annotations

import logging
from pathlib import Path

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.webui.events import UserMessageEvent
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult
from modex_agent.workspace.runtime import bind_workspace_root

logger = logging.getLogger(__name__)


class PersistUserMessageStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        # Approval decisions are structured control inputs, not user chat —
        # never persist them as user messages.  (Also guards the
        # WORKSPACE/FULL_SESSION_ID subscripts below, which a decision
        # envelope may not carry since it short-circuits workspace
        # resolution.)
        if RoutingMeta.APPROVAL_DECISION in envelope.metadata:
            return Continue(value=envelope)
        content = envelope.content.strip()
        # Defense-in-depth: a valid skill invocation legitimately starts with
        # "/" and carries skill_xml (set by S6) — it must be persisted as the
        # raw text.  Only a "/" command that is UNRESOLVED (no stage claimed
        # it) or HANDLED (a stage fully processed it, e.g. /continue) should
        # skip persisting.
        if content.startswith("/") and envelope.command_status != CommandStatus.RESOLVED:
            if envelope.command_status == CommandStatus.UNRESOLVED:
                logger.warning("Unresolved command reached persistence: %s", content)
            return Continue(value=envelope)

        full_sid = envelope.metadata[RoutingMeta.FULL_SESSION_ID]
        agent = envelope.metadata[RoutingMeta.RESOLVED_AGENT]
        pool = envelope.metadata.get(RoutingMeta.RESOLVED_POOL, "")
        attachments = [a.to_dict() for a in envelope.resolved_attachments]
        event = UserMessageEvent(
            session_id=full_sid,
            agent_name=agent,
            content=envelope.content,
            attachments=attachments,
        )
        workspace: Path = envelope.metadata[RoutingMeta.WORKSPACE]
        with bind_workspace_root(workspace):
            await ctx.transcript_store.append(full_sid, event, pool=pool)
        return Continue(value=envelope)
