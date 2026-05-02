"""ReAct graph constants — node names, transition reasons, metadata keys."""
from enum import StrEnum


class ReActNode(StrEnum):
    START = "start"
    LLM = "llm"
    TOOL = "tool"
    END = "end"


class ReActReason(StrEnum):
    NORMAL_START = "normal_start"
    RESUME_TOOLS = "resume_tools"
    HAS_TOOLS = "has_tools"
    NO_TOOLS = "no_tools"
    MAX_ITERATIONS = "max_iterations"
    LLM_ERROR = "llm_error"
    TOOLS_DONE = "tools_done"
    TURN_CANCELLED = "turn_cancelled"
    DONE = "done"


class ReActMetaKey:
    ITERATION = "_react_iteration"
    LLM_RESPONSE = "_llm_response"
    ITERATION_MSGS = "_iteration_messages"
    RESUME_STATE = "_turn_resume_state"
    TOOL_DECISIONS = "_tool_decisions"
    DENY_AS_CANCEL = "_deny_as_cancel"
    APPROVAL_DENIAL = "_approval_denial"
    INJECTION_CYCLE = "_injection_cycle_count"
    END_REASON = "_react_end_reason"
    CANCEL_REASON = "_react_cancel_reason"
    PRE_APPROVED_TOOL_IDS = "_pre_approved_tool_ids"
