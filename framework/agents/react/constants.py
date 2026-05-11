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
