"""Built-in slash command definitions for CommandDispatchStage.

Each command is an enum member + a handler function. The enum centralises the
command names (no raw strings scattered across the codebase); the handler
implements the actual behaviour. CommandDispatchStage receives a name→handler
mapping and dispatches at runtime.

Adding a new cross-channel command:
  1. Add a member to ``BuiltinCommand``.
  2. Write a ``handle_<name>`` function with the ``CommandHandler`` signature.
  3. Register it in ``SHARED_COMMANDS`` (or a pipeline-specific map).

Channel-specific commands that have positional constraints (must run before
S5, or need BYPASS_QUEUE) stay in their own stages — see
EnvironmentControlStage (S2) and SessionControlStage (S3).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from bot.input_pipeline.stages.command import CommandContext, CommandHandler
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.models import InputMessage


class BuiltinCommand(StrEnum):
    """Cross-channel built-in commands handled by CommandDispatchStage."""

    CONTINUE = "continue"


def handle_continue(c: CommandContext) -> None:
    msg = InputMessage(
        content=f"/{BuiltinCommand.CONTINUE.value}",
        session=SessionInfo.from_str(c.full_session_id),
        channel=c.envelope.channel,
        source=c.envelope.channel,
        chat_id=c.envelope.metadata.get("chat_id", ""),
        metadata={"session_id": c.full_session_id, "channel": c.envelope.channel},
        workspace=Path(c.envelope.metadata.get(RoutingMeta.WORKSPACE, str(c.ctx.current_ws()))),
    )
    c.ctx.enqueue_message(msg)


SHARED_COMMANDS: Mapping[str, CommandHandler] = {
    BuiltinCommand.CONTINUE.value: handle_continue,
}
