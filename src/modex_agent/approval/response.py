"""Compatibility helpers for approval slash-command parsing."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from modex_agent.commands.constants import BuiltinCommand, CommandParseStatus
from modex_agent.commands.parser import SlashCommandParser
from modex_agent.messaging.models import ApprovalAction


class InputCommandKind(StrEnum):
    APPROVAL = "approval"


@dataclass(frozen=True)
class ParsedInputCommand:
    kind: InputCommandKind
    approval_action: ApprovalAction | None = None


def parse_input_command(text: str) -> ParsedInputCommand | None:
    result = SlashCommandParser().parse(text)
    if result.status != CommandParseStatus.VALID_COMMAND or result.invocation is None:
        return None
    if result.invocation.command == BuiltinCommand.APPROVE.value:
        return ParsedInputCommand(
            kind=InputCommandKind.APPROVAL,
            approval_action=ApprovalAction.ALLOW,
        )
    if result.invocation.command == BuiltinCommand.DENY.value:
        return ParsedInputCommand(
            kind=InputCommandKind.APPROVAL,
            approval_action=ApprovalAction.DENY,
        )
    return None


def parse_approval_action(text: str) -> ApprovalAction | None:
    command = parse_input_command(text)
    if command is None or command.kind is not InputCommandKind.APPROVAL:
        return None
    return command.approval_action
