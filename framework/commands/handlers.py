from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Protocol

from framework.approval.types import ApprovalAction
from framework.commands.constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
    NOTICE_APPROVAL_BLOCKS_CONTINUE,
    NOTICE_INVALID_COMMAND,
    NOTICE_NO_PENDING_APPROVAL,
    NOTICE_UNKNOWN_COMMAND,
)
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
        return CommandDispatchPolicy.APPROVAL_RESPONSE

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
