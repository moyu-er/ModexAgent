"""ReAct graph constants — node names, hook/scope/event enums.

Per ADR-0033 D9.2: business modules define their own ``StrEnum`` values for
graph-runtime string parameters (``hook_point``/``scope``/``event_type``).
``StrEnum`` values are ``str`` subclasses, so they satisfy the engine's
``str`` parameter types without engine-side imports.

``ReActHookPoint`` / ``ReActScope`` / ``ReActEvent`` are the business-side
typed enums consumed by ``ReactGraphRuntime`` (Stage 1, ADR-0033 D13).
``ReActNode`` remains as the graph topology identifier. The former
``ReActReason`` enum was removed (P3.4b convergence — edges are plain
topology, routing is deliver-only).
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from modex_agent.core.message import ToolCall
from modex_agent.core.tool_manager import ToolResult


class ToolCallEndPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call: ToolCall
    result: ToolResult
    seq: int


class ReActNode(StrEnum):
    START = "start"
    LLM = "llm"
    TOOL = "tool"
    END = "end"
    BEFORE = "before"
    AFTER = "after"


class InterruptReason(StrEnum):
    """ReAct turn 中断原因枚举。

    由 ``_interrupt_reason_from`` 将控制层异常映射为短小、不泄漏内部
    细节的类别字符串，写入中断 assistant 消息的 XML 标记。``StrEnum``
    在 f-string 中序列化为 ``value``，与原有字符串字面量保持兼容。
    """

    USER_STOP = "user_stop"
    TIMEOUT = "timeout"
    POLICY = "policy"
    LOOP_DETECTED = "loop_detected"
    CANCELLED = "cancelled"
    ERROR = "error"


class ReActHookPoint(StrEnum):
    """ReAct lifecycle hook points for the graph runtime.

    These are the node-explicit hooks dispatched via
    ``ctx.runtime.dispatch_hook(ReActHookPoint.X, ctx)``. They map 1:1 to
    ``modex_agent.hook.HookPoint`` values in ``ReactGraphRuntime``.

    NOTE: ``BEFORE_TURN`` / ``AFTER_TURN`` (turn-attempt level) ARE here now —
    they are dispatched in ``BeforeTurnNode`` / ``AfterTurnNode`` via
    ``ctx.runtime.dispatch_hook()``. ``BEFORE_GRAPH`` / ``AFTER_GRAPH`` /
    ``FINALLY_GRAPH`` (graph-level) are still NOT here — they stay in
    ``actual_turn()`` dispatched via ``hook_runner.dispatch()`` directly.
    """

    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    AFTER_LLM_RESPONSE = "after_llm_response"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    FINALIZE_CONTENT = "finalize_content"
    BEFORE_LLM = "before_llm"
    START_NODE_TURN = "start_node_turn"
    END_NODE_TURN = "end_node_turn"
    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"


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
