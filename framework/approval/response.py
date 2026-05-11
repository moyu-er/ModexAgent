"""approval/response.py — parse_approval_action() 纯函数, 两套策略通用."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from framework.approval.types import ApprovalAction

_MAX_COMMAND_LENGTH = 30


class ApprovalCommandToken(StrEnum):
    APPROVE = "/approve"
    APPROVE_SHORT = "approve"
    ALLOW = "/allow"
    ALLOW_SHORT = "allow"
    YES = "/yes"
    YES_SHORT = "yes"
    OK = "/ok"
    OK_SHORT = "ok"
    DENY = "/deny"
    DENY_SHORT = "deny"
    REJECT = "/reject"
    REJECT_SHORT = "reject"
    NO = "/no"
    NO_SHORT = "no"
    CANCEL = "/cancel"
    CANCEL_SHORT = "cancel"


class InputCommandKind(StrEnum):
    APPROVAL = "approval"


@dataclass(frozen=True)
class ParsedInputCommand:
    kind: InputCommandKind
    approval_action: ApprovalAction | None = None


_APPROVE_ALIASES = frozenset(
    {
        ApprovalCommandToken.APPROVE,
        ApprovalCommandToken.APPROVE_SHORT,
        ApprovalCommandToken.ALLOW,
        ApprovalCommandToken.ALLOW_SHORT,
        ApprovalCommandToken.YES,
        ApprovalCommandToken.YES_SHORT,
        ApprovalCommandToken.OK,
        ApprovalCommandToken.OK_SHORT,
    }
)
_DENY_ALIASES = frozenset(
    {
        ApprovalCommandToken.DENY,
        ApprovalCommandToken.DENY_SHORT,
        ApprovalCommandToken.REJECT,
        ApprovalCommandToken.REJECT_SHORT,
        ApprovalCommandToken.NO,
        ApprovalCommandToken.NO_SHORT,
        ApprovalCommandToken.CANCEL,
        ApprovalCommandToken.CANCEL_SHORT,
    }
)


def parse_input_command(text: str) -> ParsedInputCommand | None:
    """Parse user text into a typed command, if it is a framework command."""

    if len(text) > _MAX_COMMAND_LENGTH:
        return None
    token = text.strip().lower()
    if token in _APPROVE_ALIASES:
        return ParsedInputCommand(
            kind=InputCommandKind.APPROVAL,
            approval_action=ApprovalAction.ALLOW,
        )
    if token in _DENY_ALIASES:
        return ParsedInputCommand(
            kind=InputCommandKind.APPROVAL,
            approval_action=ApprovalAction.DENY,
        )
    return None


def parse_approval_action(text: str) -> ApprovalAction | None:
    """将用户文本解析为审批动作。纯函数，无副作用。

    剪枝: 输入超过 30 字符直接返回 None。
    匹配: 大小写不敏感, 去除前后空白。
    """
    command = parse_input_command(text)
    if command is None or command.kind is not InputCommandKind.APPROVAL:
        return None
    return command.approval_action
