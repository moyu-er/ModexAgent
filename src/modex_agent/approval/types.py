"""审批相关的枚举类型。"""

from __future__ import annotations

from enum import StrEnum


class ApprovalTier(StrEnum):
    HARDLINE = "hardline"
    DANGEROUS = "dangerous"
    SENSITIVE = "sensitive"
    NORMAL = "normal"


class ApprovalAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class ApprovalResolution(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    TIMED_OUT = "timed_out"
    IGNORED = "ignored"
    PREEMPTED = "preempted"


class DenyAction(StrEnum):
    TOOL_ERROR = "deny_as_tool_error"
    CANCEL_TURN = "cancel_turn"


class TimeoutAction(StrEnum):
    TOOL_ERROR = "timeout_as_tool_error"
    CANCEL_TURN = "cancel_turn"


class ApprovalResultType(StrEnum):
    EXECUTED = "executed"
    RETURNED_ERROR = "returned_error"
    CANCELLED_TURN = "cancelled_turn"
