from __future__ import annotations

from enum import StrEnum


class CommandParseStatus(StrEnum):
    PLAIN_INPUT = "plain_input"
    VALID_COMMAND = "valid_command"
    INVALID_COMMAND = "invalid_command"


class CommandDispatchPolicy(StrEnum):
    NORMAL_QUEUE = "normal_queue"
    APPROVAL_RESPONSE = "approval_response"
    BYPASS_QUEUE = "bypass_queue"
    DROP_IF_BUSY = "drop_if_busy"


class CommandAction(StrEnum):
    NOOP = "noop"
    NOTICE = "notice"
    TRANSFORM_TO_USER_INPUT = "transform_to_user_input"
    CONTINUE_AGENT = "continue_agent"
    APPROVAL_DECISION = "approval_decision"
    CONTROL_COMMAND = "control_command"


class BuiltinCommand(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    CONTINUE = "continue"


NOTICE_INVALID_COMMAND = "Invalid command syntax. Commands must look like /command or /command text."
NOTICE_UNKNOWN_COMMAND = "Unknown command: /{command}"
NOTICE_NO_PENDING_APPROVAL = "No pending approval request."
NOTICE_APPROVAL_BLOCKS_CONTINUE = "A pending approval request exists. Use /approve or /deny first."
NOTICE_COMMAND_FAILED = "Command failed: {reason}"
