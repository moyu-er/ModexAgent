from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Protocol

from framework.approval.types import ApprovalAction
from framework.commands.constants import (
    NOTICE_APPROVAL_BLOCKS_CONTINUE,
    NOTICE_INVALID_COMMAND,
    NOTICE_NO_PENDING_APPROVAL,
    NOTICE_SKILL_NOT_FOUND,
    NOTICE_UNKNOWN_COMMAND,
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
)
from xml.sax.saxutils import escape as xml_escape

from framework.memory.core.message import ContentFormat
from framework.commands.models import (
    CommandContext,
    CommandHandlingResult,
    SlashCommandInvocation,
)

logger = logging.getLogger(__name__)


class CommandHandler(Protocol):
    @property
    def names(self) -> Collection[str]:
        ...

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        ...

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        ...


class ApprovalCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.APPROVE.value, BuiltinCommand.DENY.value)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        if context.pending_approval is not None:
            return CommandDispatchPolicy.APPROVAL_RESPONSE
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        if invocation.args:
            logger.info("Ignoring args for approval command: /%s", invocation.command)
        if context.pending_approval is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.APPROVAL_RESPONSE,
                notice=NOTICE_NO_PENDING_APPROVAL,
                invocation=invocation,
            )
        approval_action = (
            ApprovalAction.ALLOW
            if invocation.command == BuiltinCommand.APPROVE.value
            else ApprovalAction.DENY
        )
        return CommandHandlingResult(
            action=CommandAction.APPROVAL_DECISION,
            dispatch_policy=CommandDispatchPolicy.APPROVAL_RESPONSE,
            approval_action=approval_action,
            invocation=invocation,
        )


class ContinueCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return (BuiltinCommand.CONTINUE.value,)

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        if invocation.args:
            logger.info("Ignoring args for /continue")
        if context.pending_approval is not None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice=NOTICE_APPROVAL_BLOCKS_CONTINUE,
                invocation=invocation,
            )
        return CommandHandlingResult(
            action=CommandAction.CONTINUE_AGENT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            append_user_message=False,
            trigger_agent=True,
            invocation=invocation,
        )


class UnknownCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return ()

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=NOTICE_UNKNOWN_COMMAND.format(command=invocation.command),
            invocation=invocation,
        )


class InvalidCommandHandler:
    async def handle_parse_error(
        self,
        error: str,
        context: CommandContext,
    ) -> CommandHandlingResult:
        logger.info("Invalid slash command: %s", error)
        return CommandHandlingResult(
            action=CommandAction.NOTICE,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            notice=NOTICE_INVALID_COMMAND,
        )


def build_default_builtin_handlers() -> Sequence[CommandHandler]:
    return (ApprovalCommandHandler(), ContinueCommandHandler())


class SkillCommandHandler:
    @property
    def names(self) -> Collection[str]:
        return ()

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.NORMAL_QUEUE

    async def can_handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> bool:
        if invocation.command in {c.value for c in BuiltinCommand}:
            return False
        if context.skill_manager is None:
            return False
        return await context.skill_manager.get_skill(invocation.command) is not None

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        if context.skill_manager is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice=NOTICE_UNKNOWN_COMMAND.format(command=invocation.command),
                invocation=invocation,
            )
        skill = await context.skill_manager.get_skill(invocation.command)
        if skill is None:
            return CommandHandlingResult(
                action=CommandAction.NOTICE,
                dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
                notice=NOTICE_SKILL_NOT_FOUND.format(command=invocation.command),
                invocation=invocation,
            )
        content = (
            f'<command_context type="skill" name="{xml_escape(skill.name)}">\n'
            f"<skill>\n{xml_escape(skill.content)}\n</skill>\n"
            f"</command_context>\n\n"
            f"<user_input>\n{xml_escape(invocation.args)}\n</user_input>"
        )
        logger.info("Resolved slash skill command: /%s", invocation.command)
        return CommandHandlingResult(
            action=CommandAction.TRANSFORM_TO_USER_INPUT,
            dispatch_policy=CommandDispatchPolicy.NORMAL_QUEUE,
            user_content=content,
            append_user_message=True,
            trigger_agent=True,
            invocation=invocation,
            metadata={"skill_name": skill.name, "skill_location": skill.location or ""},
            content_format=ContentFormat.XML,
            truncatable_paths=["user_input"],
        )
