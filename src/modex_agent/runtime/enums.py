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
    PRE_APPROVED_TOOL_IDS = "_pre_approved_tool_ids"
    APPROVAL_YOLO = "approval_yolo"
    POLICY_DENIED_TOOLS = "_policy_denied_tools"
    TOOL_USAGE = "usage"
    GRAPH_RESULT = "_graph_result"
    MAX_TOOLS_PER_TURN = "max_tools_per_turn"
    TURN_UUID = "_turn_uuid"
    INJECTION_CYCLE_COUNT = "_injection_cycle_count"
    TRACE_ID = "_trace_id"
    # Root invoke_agent span_id for the current turn (G10 multi-agent handoff).
    # Set by TraceCollectorHook.before_graph; read by AgentCommunicationService
    # to link the agent.handoff span's parent_span_id to the turn's root span.
    ROOT_SPAN_ID = "_root_span_id"
    # Resolved image-kind Attachment records for the current turn (ADR-0014 §3 /
    # OpenSpec native-multimodal-inline unit 3). Path-only VOs (path/mime/kind/
    # name/size) — never bytes. Read by the inline renderer (unit 4) to bind
    # vision blocks; base64 is materialized lazily in unit 5.
    INLINE_ATTACHMENTS = "inline_attachments"
    # Per-turn cache of already-rendered image content blocks keyed by the
    # attachment id (ADR-0014 §5 / OpenSpec native-multimodal-inline unit 5).
    # Value: dict[str, list[dict]] mapping att.id -> the 2-element caption +
    # image_url block list, so base64 is encoded once per turn and reused
    # across ReAct iterations. Lives only in turn state — never persisted.
    INLINE_IMAGE_CACHE = "_inline_image_cache"
    # Per-turn cache of image content blocks produced by TOOLS (e.g. ReadFileTool
    # reading an image file), keyed by tool_call_id. Mirrors INLINE_IMAGE_CACHE
    # (which is keyed by attachment id for user-uploaded attachments). Value:
    # dict[str, ToolMediaEntry] mapping call_id -> entry (carries tool_name +
    # image_blocks for per-call attribution in the synthetic user message).
    # Lives only in turn state — never persisted. Read by enrich_inline_media
    # which delegates to a ToolResultMediaStrategy (default
    # SyntheticUserMessageStrategy — Path B) to inject a synthetic user message
    # after tool results.
    TOOL_MEDIA_CACHE = "_tool_media_cache"
    CONTINUATION_REQUEST = "_continuation_request"
    # One-shot flag: a hook with progress-driven continuation (currently only
    # TodoContinuationHook) authorizes the gate to renew MAX_TURNS past the
    # current upper bound.  Gate pops this alongside CONTINUATION_REQUEST and
    # increments MAX_TURNS by 1 only once regardless of how many hooks set it.
    CONTINUATION_RENEW_MAX_TURNS = "_continuation_renew_max_turns"
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
    GRAPH_NODE_DESCRIPTION = "_graph_node_description"
    # Serialized graph topology (markdown) for the ### Topology subsection
    # of ## Graph Node Context in the system prompt. Set by BotAgentNode.execute
    # before execute_turn; read by GraphWorkflowProvider._fetch_content.
    # Empty string when graph_ref is None (test/no-scheduler path).
    GRAPH_TOPOLOGY_CONTEXT = "_graph_topology_context"
