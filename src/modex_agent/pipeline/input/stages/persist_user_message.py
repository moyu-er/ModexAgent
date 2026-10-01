"""S7: persist the raw user message through the injected transcript writer.

The single user-message persistence point (the guards below are the S7
contract): approval decisions are structured control inputs, never user
chat, and a slash command that no stage claimed (UNRESOLVED) or that a
stage fully handled (HANDLED) is not persisted. The transcript event shape
and its store are deployment-owned — the bot's writer builds its
``UserMessageEvent`` and appends through its transcript store under the
resolved workspace root.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from modex_agent.pipeline.input.context import InputContext
from modex_agent.pipeline.input.envelope import CommandStatus, UserInputEnvelope
from modex_agent.pipeline.input.stage import Continue, InputStage, StageResult
from modex_agent.pipeline.input.stages.resolve_pool import RoutingMeta

logger = logging.getLogger(__name__)

#: Deployment-injected transcript writer: persists the (already-guarded)
#: envelope's user message. Runs only for inputs that passed the S7 guards.
UserMessageWriter = Callable[[UserInputEnvelope, InputContext], Awaitable[None]]


class PersistUserMessageStage(InputStage):
    """Run the S7 guards, then hand the envelope to the injected writer."""

    def __init__(self, writer: UserMessageWriter) -> None:
        self._writer = writer

    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
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

        await self._writer(envelope, ctx)
        return Continue(value=envelope)
