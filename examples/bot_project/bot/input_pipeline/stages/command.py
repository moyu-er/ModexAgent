"""Shared stage: dispatch built-in slash commands via a configurable handler map.

Both IM and WebUI pipelines use this stage, each passing its own set of
command handlers. The stage is a pure dispatcher — it looks up the first
token in the handler map and delegates. Unrecognised input passes through.

Handler functions and command enums live in ``commands.py``; this module
only owns the dispatch mechanism (the ``CommandDispatchStage`` class and the
``CommandContext`` / ``CommandHandler`` types).

Channel-specific commands that have positional constraints (must run before
S5, or need BYPASS_QUEUE) stay in their own stages:
  - IM-only (/cd, /pool, /exit, /pwd) → EnvironmentControlStage (S2)
  - IM-only (/stop)                   → SessionControlStage (S3)
  - Skills (/skillName)               → SkillParseStage (S6)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import resolve_session_routing
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult


@dataclass
class CommandContext:
    envelope: UserInputEnvelope
    ctx: BotInputContext
    full_session_id: str


CommandHandler = Callable[[CommandContext], None]


class CommandDispatchStage(InputStage):
    """Dispatch slash commands via a caller-supplied handler mapping.

    ``handlers`` maps a command name (lowercased, no leading '/') to a
    ``CommandHandler``. The stage is inert without handlers — pipelines
    declare exactly which commands they support.
    """

    def __init__(self, handlers: Mapping[str, CommandHandler]) -> None:
        self._handlers = dict(handlers)

    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        content = (envelope.content or "").strip()
        if not content.startswith("/"):
            return Continue(value=envelope)

        token = content[1:].split(None, 1)[0].lower()
        handler = self._handlers.get(token)
        if handler is None:
            return Continue(value=envelope)

        _, _, full_sid = resolve_session_routing(envelope, ctx)
        handler(CommandContext(envelope=envelope, ctx=ctx, full_session_id=full_sid))
        envelope.command_status = CommandStatus.HANDLED
        return Continue(value=envelope)
