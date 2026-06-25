"""ReAct graph constants — node names, transition reasons, metadata keys."""

from enum import StrEnum

from modex_agent.core.constants import StopReason


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
    TOOLS_DONE = "tools_done"
    LLM_ERROR = "llm_error"
    DONE = "done"
    # Turn-ending reasons aligned with StopReason
    MAX_ITERATIONS = StopReason.MAX_ITERATIONS
    TURN_CANCELLED = StopReason.TURN_CANCELLED
