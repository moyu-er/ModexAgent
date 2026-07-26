"""Terminal command-resolution stage: reject any unclaimed slash command.

Runs after every claiming stage (approval, skill, command dispatch, IM
control) and before persistence/enqueue. By here, every SUPPORTED command
has been claimed and its ``command_status`` set to RESOLVED or HANDLED. So a
"/command" that is still UNRESOLVED is, by definition, unsupported — return
the single generic notice.
"""

from __future__ import annotations

from bot.input_pipeline.context import BotInputContext
from modex_agent.commands.constants import NOTICE_UNKNOWN_COMMAND
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult, Terminate


class UnsupportedCommandStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        content = (envelope.content or "").strip()
        if not content.startswith("/") or envelope.command_status != CommandStatus.UNRESOLVED:
            return Continue(value=envelope)
        command_name = content[1:].split(None, 1)[0]
        return Terminate(
            reason="unsupported_command",
            response={"message": NOTICE_UNKNOWN_COMMAND.format(command=command_name)},
        )
