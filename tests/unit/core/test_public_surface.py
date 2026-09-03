from __future__ import annotations

import modex_agent.core as core

CORE_PUBLIC = [
    "MessageRole",
    "FinishReason",
    "StopReason",
    "ToolCall",
    "ToolResult",
    "TurnEvent",
    "TurnReasoningEvent",
    "TurnTextEvent",
    "TurnToolCallEvent",
    "TurnToolResultEvent",
    "EventAssembler",
    "ReplayFields",
    "TextDelta",
    "ReasoningDelta",
    "ToolCallComplete",
    "UsageSnapshot",
    "Finish",
    "StreamFailure",
    "LLMStreamEvent",
    "AgentEvent",
    "EmitterConfig",
    "AgentResult",
    "ContentEmitter",
    "ToolManager",
    "Tool",
    "ToolConfig",
    "ToolExecutionContext",
    "get_tool_execution_context",
    "ExecutionMode",
    "ParallelTool",
    "ExclusiveTool",
    "Modality",
    "ModelCapabilities",
    "ModelInfo",
    "Agent",
    "AgentCommKind",
    "AgentContext",
    "AgentRole",
    "ExecutionStrategyKind",
    "ProviderKind",
    "MessageHistory",
    "LLMProvider",
    "CallbackStreamProvider",
    "current_agent_context",
    "SessionInfo",
    "SessionIdFactory",
    "agent_of",
    "session_id_prefix_of",
    "encode_snowflake",
    "ChatMessage",
    "ContentFormat",
    "LLMRequest",
    "LLMResponse",
    "ReasoningEffort",
    "TokenUsage",
    "RuntimeSafetyPolicy",
    "SystemPromptProvider",
    "SystemPromptPipeline",
    "RecordScope",
    "Attachment",
    "AttachmentLocator",
    "Kind",
    "MediaRefCollisionError",
    "MediaStore",
    "StoredFile",
    "StoredMediaKind",
]

MOVED_FEATURE_EXPORTS = {
    # Memory context and governance implementations.
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "CompositeScope",
    "ContextGovernance",
    "ContextManager",
    "ContextState",
    "GlobalScope",
    "InMemoryContextManager",
    "ListMessageHistory",
    "MemoryContext",
    "RuntimeInfoKey",
    "ScopedMessageHistory",
    "SessionScope",
    "TenantScope",
    "UserScope",
    "format_working_directory_line",
    # Messaging transport models.
    "ApprovalAction",
    "ApprovalDecisionInput",
    "InputMessage",
    "MessageType",
    "OutputMessage",
    "OutputMessageType",
    "ReminderKind",
    # Persistence contracts and implementations.
    "InMemorySessionRegistry",
    "LocalFileSessionStore",
    "SessionRegistry",
    "SessionStore",
    "safe_filename",
    # ReAct-only fallback id generation.
    "next_call_id",
    # Todo runtime values and stores.
    "JsonFileTodoStore",
    "TodoItem",
    "TodoStatus",
    "TodoStore",
}


def test_core_exports_exact_public_surface() -> None:
    assert core.__all__ == CORE_PUBLIC
    missing = [n for n in CORE_PUBLIC if not hasattr(core, n)]
    assert not missing, f"core facade missing: {missing}"


def test_core_does_not_export_moved_feature_concretes() -> None:
    offenders = sorted(name for name in MOVED_FEATURE_EXPORTS if hasattr(core, name))
    assert not offenders, f"core facade still exports moved feature symbols: {offenders}"


def test_message_history_is_exposed_only_from_core_facade() -> None:
    import modex_agent.memory as memory
    from modex_agent.core import MessageHistory

    assert core.MessageHistory is MessageHistory
    assert "MessageHistory" not in memory.__all__
    assert not hasattr(memory, "MessageHistory")
