from __future__ import annotations

import logging
from collections.abc import Sequence

from modex_agent.commands.constants import CommandAction, CommandDispatchPolicy, CommandParseStatus
from modex_agent.commands.handlers import (
    CommandHandler,
    InvalidCommandHandler,
    SkillCommandHandler,
    UnknownCommandHandler,
    build_default_builtin_handlers,
)
from modex_agent.commands.models import (
    CommandContext,
    CommandHandlingResult,
    CommandParseResult,
    CommandProcessor,
    SlashCommandInvocation,
)
from modex_agent.commands.parser import SlashCommandParser

logger = logging.getLogger(__name__)


class SlashCommandProcessor(CommandProcessor):
    def __init__(
        self,
        *,
        parser: SlashCommandParser | None = None,
        handlers: Sequence[CommandHandler] | None = None,
        skill_handler: SkillCommandHandler | None = None,
        invalid_handler: InvalidCommandHandler | None = None,
        unknown_handler: UnknownCommandHandler | None = None,
    ) -> None:
        self._parser = parser or SlashCommandParser()
        self._handlers = list(handlers or ())
        self._skill_handler = skill_handler or SkillCommandHandler()
        self._invalid_handler = invalid_handler or InvalidCommandHandler()
        self._unknown_handler = unknown_handler or UnknownCommandHandler()
        self._handler_by_name = {
            name: handler for handler in self._handlers for name in handler.names
        }

    @classmethod
    def default(cls) -> SlashCommandProcessor:
        return cls(handlers=build_default_builtin_handlers())

    def parse(self, text: str) -> CommandParseResult:
        return self._parser.parse(text)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        handler = self._handler_by_name.get(invocation.command)
        if handler is not None:
            return handler.dispatch_policy(invocation, context)
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        text: str,
        context: CommandContext,
    ) -> CommandHandlingResult:
        parse_result = self.parse(text)
        if parse_result.status == CommandParseStatus.PLAIN_INPUT:
            return CommandHandlingResult(
                action=CommandAction.NOOP,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                trigger_agent=True,
                append_user_message=True,
            )
        if parse_result.status == CommandParseStatus.INVALID_COMMAND:
            return await self._invalid_handler.handle_parse_error(
                parse_result.error or "invalid slash-command syntax",
                context,
            )

        invocation = parse_result.invocation
        if invocation is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice="Invalid command syntax. Commands must look like /command or /command text.",
            )

        handler = self._handler_by_name.get(invocation.command)
        if handler is not None:
            logger.info("Handling builtin slash command: /%s", invocation.command)
            return await handler.handle(invocation, context)

        return await self._skill_handler.handle(invocation, context)
