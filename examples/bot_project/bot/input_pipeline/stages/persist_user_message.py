"""Bot transcript writer injected into the framework S7 stage.

The framework :class:`~modex_agent.pipeline.input.stages.persist_user_message.PersistUserMessageStage`
owns the S7 guards (approval decisions and unclaimed/handled slash commands
never persist); this module owns the bot's write shape — the
``UserMessageRecord`` appended through the context's transcript store
under the envelope's resolved workspace root.
"""

from __future__ import annotations

import time
from pathlib import Path

from bot.input_pipeline.context import BotInputContext
from bot.webui.transcript_store import UserMessageRecord
from modex_agent.pipeline.input.context import InputContext
from modex_agent.pipeline.input.envelope import UserInputEnvelope
from modex_agent.pipeline.input.stages.resolve_pool import RoutingMeta
from modex_agent.workspace.runtime import bind_workspace_root


async def write_user_message_to_transcript(
    envelope: UserInputEnvelope, ctx: InputContext
) -> None:
    """Persist the guarded user message to the bot's transcript store."""
    assert isinstance(ctx, BotInputContext), (
        "the bot user-message writer requires BotInputContext"
    )
    full_sid = envelope.metadata[RoutingMeta.FULL_SESSION_ID]
    agent = envelope.metadata[RoutingMeta.RESOLVED_AGENT]
    pool = envelope.metadata.get(RoutingMeta.RESOLVED_POOL, "")
    attachments = [a.to_dict() for a in envelope.resolved_attachments]
    record = UserMessageRecord(
        session_id=full_sid,
        agent_name=agent,
        timestamp_ms=int(time.time() * 1000),
        content=envelope.content,
        attachments=attachments,
    )
    workspace: Path = envelope.metadata[RoutingMeta.WORKSPACE]
    with bind_workspace_root(workspace):
        await ctx.transcript_store.append(full_sid, record, pool=pool)
