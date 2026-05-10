"""Runtime state governance enums.

All protocol values for runtime state management use StrEnum — no ad hoc strings.
"""

from __future__ import annotations

from enum import StrEnum


class StateScope(StrEnum):
    PROCESS = "process"
    AGENT = "agent"
    SESSION = "session"
    TURN = "turn"
    OPERATION = "operation"


class AgentKind(StrEnum):
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"


class TurnPhase(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationKind(StrEnum):
    LLM_CALL = "llm_call"
    TOOL_BATCH = "tool_batch"
    TOOL_CALL = "tool_call"
    APPROVAL = "approval"
    CONTROL_COMMAND = "control_command"


class OperationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class MessageDeltaSource(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    SYSTEM = "system"


class ToolBatchStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    PREEMPTED = "preempted"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancellationSource(StrEnum):
    USER_COMMAND = "user_command"
    TIMEOUT = "timeout"
    POLICY = "policy"
    TOOL_DENIAL = "tool_denial"
    CONTROL_COMMAND = "control_command"


class ControlCommandKind(StrEnum):
    CANCEL_TURN = "cancel_turn"
    PAUSE_TURN = "pause_turn"
    RESUME_TURN = "resume_turn"
    INJECT_INPUT = "inject_input"


class SnapshotReason(StrEnum):
    LLM_COMPLETED = "llm_completed"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    TOOL_BATCH_PROGRESS = "tool_batch_progress"
    TURN_INTERRUPTED = "turn_interrupted"
    ERROR_RECOVERY = "error_recovery"


class ApprovalSubjectType(StrEnum):
    TOOL_CALL = "tool_call"
    TOOL_BATCH = "tool_batch"
    PLAN_STEP = "plan_step"


class ApprovalDenyPolicy(StrEnum):
    TOOL_RESULT_ONLY = "tool_result_only"
    CANCEL_TURN = "cancel_turn"


class TurnCustomKey(StrEnum):
    """Keys for ``TurnStateBase.custom`` per-turn state used by hooks and interceptors.

    Hooks and interceptors store lightweight per-turn data in ``TurnStateBase.custom``
    using these typed keys. Values must be JSON-serializable if turn snapshots are
    persisted.
    """

    STREAM_CANCELLED = "_stream_cancelled"
    CANCEL_COMMAND_TYPE = "_cancel_cmd_type"
    CANCELLED_TOOL_RECORDS = "_cancelled_tool_records"
    LLM_OUTPUT_RISK = "_llm_output_risk"
    CONSECUTIVE_ERRORS = "consecutive_errors"
    DYNAMIC_TOOL_ACTIVE = "_dynamic_tool_active"
    DYNAMIC_TOOL_DENIED = "_dynamic_tool_denied"
    PRE_APPROVED_TOOL_IDS = "_pre_approved_tool_ids"
    APPROVAL_YOLO = "approval_yolo"
    POLICY_DENIED_TOOLS = "_policy_denied_tools"
    TOOL_USAGE = "usage"
