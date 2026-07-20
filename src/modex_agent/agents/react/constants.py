"""ReAct graph constants — node names, transition reasons, hook/scope/event enums.

Per ADR-0033 D9.2: business modules define their own ``StrEnum`` values for
graph-runtime string parameters (``hook_point``/``scope``/``event_type``).
``StrEnum`` values are ``str`` subclasses, so they satisfy the engine's
``str`` parameter types without engine-side imports.

``ReActHookPoint`` / ``ReActScope`` / ``ReActEvent`` are the business-side
typed enums consumed by ``ReactGraphRuntime`` (Stage 1, ADR-0033 D13).
``ReActNode`` / ``ReActReason`` remain as graph topology identifiers.
"""

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


class ReActHookPoint(StrEnum):
    """ReAct lifecycle hook points for the graph runtime.

    These are the node-explicit hooks dispatched via
    ``ctx.runtime.dispatch_hook(ReActHookPoint.X, ctx)``. They map 1:1 to
    ``modex_agent.hook.HookPoint`` values in ``ReactGraphRuntime``.

    NOTE: ``BEFORE_TURN`` / ``AFTER_TURN`` / ``FINALLY_TURN`` are NOT here —
    they are turn-level hooks dispatched in ``ReActAgent.run()`` directly
    via ``hook_runner.dispatch(HookPoint.X, agent_ctx)``, not through the
    graph runtime. This preserves hook timing exactly (ADR-0033 D5 rule 1).
    """

    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    AFTER_LLM_RESPONSE = "after_llm_response"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    FINALIZE_CONTENT = "finalize_content"


class ReActScope(StrEnum):
    """ReAct interceptor scopes for the graph runtime.

    These map to ``modex_agent.interceptor.InterceptorScope`` values in
    ``ReactGraphRuntime.around()``.

    NOTE: ``TURN`` scope is NOT here — ``around_turn`` is dispatched in
    ``ReActAgent.run()`` directly, not through the graph runtime.
    """

    ITERATION = "iteration"
    LLM_CALL = "llm_call"
    LLM_STREAM = "llm_stream"
    TOOL_CALL = "tool_call"


class ReActEvent(StrEnum):
    """ReAct streaming events for the graph runtime.

    These are the events dispatched via ``ctx.runtime.emit(ReActEvent.X,
    data, ctx)``. They map to the existing ``ReActEvent`` enum in
    ``modex_agent.agents.react.agent`` (which inherits ``AgentEvent`` and
    includes additional events like ``MODEL_REASONING`` / ``ITERATION_START``
    that are emitted directly via ``ctx.emitter.emit()``, not through the
    graph runtime).

    The 9 values here are the subset that goes through the graph runtime's
    ``emit`` method. The existing ``agent.ReActEvent`` enum is a superset.
    """

    START = "start"
    MAX_ITERATIONS = "max_iterations"
    MODEL_OUTPUT = "model_output"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    ITERATION_END = "iteration_end"
    PROGRESS = "progress"
    FINAL_OUTPUT = "final_output"
    ERROR = "error"
