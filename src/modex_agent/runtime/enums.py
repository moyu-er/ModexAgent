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
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    ERROR = "error"


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


class SnapshotReason(StrEnum):
    LLM_COMPLETED = "llm_completed"
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
    TOOL_BATCH_PROGRESS = "tool_batch_progress"
    TURN_INTERRUPTED = "turn_interrupted"
    ERROR_RECOVERY = "error_recovery"
    ITERATION = "iteration"


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
    # Partial assistant content captured when an LLM stream is interrupted
    # mid-flight (user /stop, pause, timeout, error). The agent's cancel/error
    # handler reads it to persist an XML-marked interrupted message so memory
    # stays aligned with the transcript. Value: {"content": str, "tool_names": list[str]}.
    INTERRUPTED_PARTIAL = "_interrupted_partial"
    LLM_OUTPUT_RISK = "_llm_output_risk"
    CONSECUTIVE_ERRORS = "consecutive_errors"
    DYNAMIC_TOOL_ACTIVE = "_dynamic_tool_active"
    DYNAMIC_TOOL_DENIED = "_dynamic_tool_denied"
    APPROVAL_YOLO = "approval_yolo"
    POLICY_DENIED_TOOLS = "_policy_denied_tools"
    TOOL_USAGE = "usage"
    GRAPH_RESULT = "_graph_result"
    MAX_TOOLS_PER_TURN = "max_tools_per_turn"
    TURN_UUID = "_turn_uuid"
    INJECTION_CYCLE_COUNT = "_injection_cycle_count"
    TRACE_ID = "_trace_id"
    # Root invoke_agent span_id for the current turn (G10 multi-agent handoff).
    # Set by RootSpanHook.start_node_turn for child span parentage.
    ROOT_SPAN_ID = "_root_span_id"
    # Parent span ID for subagent trace linking (G10 multi-agent handoff).
    # When a subagent's trace should link to a parent span other than the
    # turn root, the parent's span ID is stored here. Read by
    # AgentCommunicationService to construct the child trace's parent_span_id.
    PARENT_SPAN_ID = "parent_span_id"
    # The parent agent's handoff span ID, stored on the child's turn state
    # so the child can reference the specific handoff span that spawned it.
    HANDOFF_SPAN_ID = "handoff_span_id"
    # TrajectoryMetrics for the finished turn, derived by RootSpanHook from
    # the TraceSessionState scalar counters at finally_graph (BEFORE
    # clear_trace pops the counters bucket). Value: the frozen
    # TrajectoryMetrics model. Read by eval-side turn aggregation so it
    # never reads spans back from the trace store.
    TRAJECTORY_METRICS = "trajectory_metrics"
    CONTINUATION_REQUEST = "_continuation_request"
    # One-shot flag: a hook with progress-driven continuation (currently only
    # TodoContinuationHook) authorizes the gate to renew MAX_TURNS past the
    # current upper bound.  Gate pops this alongside CONTINUATION_REQUEST and
    # increments MAX_TURNS by 1 only once regardless of how many hooks set it.
    CONTINUATION_RENEW_MAX_TURNS = "_continuation_renew_max_turns"
    # FinishReason of the last LLM response in this turn, recorded by
    # LengthGuardHook.after_llm_response (StrEnum value — JSON-safe) and read
    # by LengthGuardHook.after_turn to detect degenerate endings.
    LAST_LLM_FINISH_REASON = "last_llm_finish_reason"
    # Consecutive degenerate-ending counter maintained by LengthGuardHook.
    # Incremented on each degenerate ending, reset to 0 by any productive
    # LLM response (content or tool calls). Exhaustion at MAX_NUDGES.
    LENGTH_GUARD_NUDGES = "length_guard_nudges"
    # Loop-detection episode for the current turn, maintained by
    # LoopDetectionHook.before_iteration. Value: {"fp": str, "rounds": int} —
    # the tool-call identity (canonical preview string) that triggered the
    # advisory reminder, and the trailing-run length at injection. Cleared
    # whenever the trailing identity changes (loop broken / switched);
    # consumed when the same identity persists observation_rounds past the
    # injection (hard exit). Transient: an approval suspend/resume dropping
    # it only costs one re-reminder.
    LOOP_EPISODE = "_loop_episode"
    LAST_CONTINUATION_TODO_SIG = "_last_continuation_todo_sig"
    GRAPH_DELIVER_COUNT = "_graph_deliver_count"
    MAX_TURNS = "_max_turns"
    # Per-turn counter for graph knowledge base read actions (read/grep).
    # Incremented by GraphKnowledgeBaseTool; read and reset by KnowledgeHook
    # each turn attempt.
    GRAPH_KNOWLEDGE_READ_COUNT = "_graph_knowledge_read_count"
    # Per-turn counter for graph knowledge base write actions (write/edit).
    # Incremented by GraphKnowledgeBaseTool; read and reset by KnowledgeHook
    # each turn attempt.
    GRAPH_KNOWLEDGE_WRITE_COUNT = "_graph_knowledge_write_count"
    # Per-turn path (string) to the graph instance knowledge directory.
    # Set by BotAgentNode.execute; read by KnowledgeHook to inject
    # findings/open_questions summaries at turn start.
    GRAPH_KNOWLEDGE_DIR = "_graph_knowledge_dir"
    # Per-turn per-node knowledge requirement flags (bools, default False).
    # Set by BotAgentNode.execute from NodeSpec config; read by
    # KnowledgeHook to decide whether continuation is needed.
    GRAPH_KNOWLEDGE_REQUIRE_READ = "_graph_knowledge_require_read"
    GRAPH_KNOWLEDGE_REQUIRE_WRITE = "_graph_knowledge_require_write"
    # Whether the KB had readable content (findings.md or open_questions.md
    # existing and non-empty) at turn start. Set by KnowledgeHook.before_turn
    # after injecting the summary; read by KnowledgeHook.after_turn to decide
    # whether require_read is enforceable — when the KB has no prior
    # contributions, require_read is exempted (the agent cannot read what no
    # node has written).
    GRAPH_KNOWLEDGE_HAS_READABLE = "_graph_knowledge_has_readable"
    GRAPH_NODE_DESCRIPTION = "_graph_node_description"
    # Serialized graph topology (markdown) for the ### Topology subsection
    # of ## Graph Node Context in the system prompt. Set by BotAgentNode.execute
    # before execute_turn; read by GraphWorkflowProvider._fetch_content.
    # Empty string when graph_ref is None (test/no-scheduler path).
    GRAPH_TOPOLOGY_CONTEXT = "_graph_topology_context"
    # Whether the current graph node has at least one AgentNode as a direct
    # downstream target. Set by GraphTopologyConfigurator from
    # GraphTurnArtifacts; read by GraphWorkflowProvider to conditionally
    # render Producer/Relay deliver patterns.
    GRAPH_DOWNSTREAM_HAS_AGENT = "_graph_downstream_has_agent"
    # Whether the current graph node has __end__ as a direct downstream
    # target. Set by GraphTopologyConfigurator from GraphTurnArtifacts;
    # read by GraphWorkflowProvider to conditionally render the Final
    # Reply deliver pattern.
    GRAPH_DOWNSTREAM_HAS_END = "_graph_downstream_has_end"
