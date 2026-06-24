from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Collection, Sequence

from modex_agent.approval.types import ApprovalAction
from modex_agent.commands.constants import (
    NOTICE_APPROVAL_BLOCKS_CONTINUE,
    NOTICE_INVALID_COMMAND,
    NOTICE_NO_PENDING_APPROVAL,
    NOTICE_SKILL_NOT_FOUND,
    NOTICE_UNKNOWN_COMMAND,
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
)
from modex_agent.commands.models import (
    CommandContext,
    CommandHandlingResult,
    SlashCommandInvocation,
)
from modex_agent.core.skills.builder import build_skill_command_xml
from modex_agent.memory.core.message import ContentFormat

logger = logging.getLogger(__name__)


class CommandHandler(ABC):
    @property
    @abstractmethod
    def names(self) -> Collection[str]: ...

    @abstractmethod
    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy: ...

    @abstractmethod
    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult: ...


class ApprovalCommandHandler(CommandHandler):
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


class ContinueCommandHandler(CommandHandler):
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


class UnknownCommandHandler(CommandHandler):
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


class ControlCommandHandler(CommandHandler):
    """Handles /stop and future control slash commands.

    Returns CONTROL_COMMAND action for the pipeline to execute immediately
    via task.cancel() in the pre-lock phase.
    """

    _COMMAND_MAP: dict[str, str] = {
        "stop": "cancel_turn",
    }

    @property
    def names(self) -> Collection[str]:
        return tuple(self._COMMAND_MAP.keys())

    def dispatch_policy(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandDispatchPolicy:
        return CommandDispatchPolicy.BYPASS_QUEUE

    async def handle(
        self,
        invocation: SlashCommandInvocation,
        context: CommandContext,
    ) -> CommandHandlingResult:
        from uuid import uuid4

        from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope

        cmd_type_str = self._COMMAND_MAP[invocation.command]
        cmd_type = ControlCommandType(cmd_type_str)

        notice_map: dict[str, str] = {
            "stop": "⏹ Agent turn stopped.",
        }
        notice = notice_map.get(invocation.command)

        control_cmd = ControlCommand(
            command_id=uuid4().hex,
            type=cmd_type,
            scope=ControlScope(session_id=context.session_id),
            source="user:slash",
        )

        return CommandHandlingResult(
            action=CommandAction.CONTROL_COMMAND,
            dispatch_policy=CommandDispatchPolicy.BYPASS_QUEUE,
            control_command=control_cmd,
            notice=notice,
            invocation=invocation,
        )


def build_default_builtin_handlers() -> Sequence[CommandHandler]:
    return (ApprovalCommandHandler(), ContinueCommandHandler(), ControlCommandHandler())


class SkillCommandHandler(CommandHandler):
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
            logger.warning(
                "SkillCommandHandler: skill_manager is None, cannot resolve /%s "
                "(pipeline skill_manager not wired or _build_pool_skill_manager returned None)",
                invocation.command,
            )
            return False
        skill = await context.skill_manager.get_skill(invocation.command)
        if skill is None:
            logger.info(
                "SkillCommandHandler: skill '/%s' not found by SkillManager.get_skill",
                invocation.command,
            )
            return False
        return True

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
        content = build_skill_command_xml(skill.name, skill.content, invocation.args)
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
