from __future__ import annotations

import modex_agent.core as core

CORE_PUBLIC = [
    "Agent", "AgentContext", "AgentCommKind",
    "SessionInfo", "SessionIdFactory", "now_ms", "agent_of",
    "session_id_prefix_of", "encode_snowflake",
    "SessionStore", "LocalFileSessionStore", "safe_filename",
    "SessionRegistry", "InMemorySessionRegistry",
    "ChatMessage", "ContentFormat",
    "InputMessage", "OutputMessage", "MessageRole", "ToolCall", "LLMResponse", "TodoStatus",
    "EventAssembler",
    "RuntimeSafetyPolicy", "SystemPromptPipeline",
    "current_agent_context",
    "Attachment", "AttachmentLocator", "Kind",
    "MediaStore", "StoredFile", "StoredMediaKind", "MediaRefCollisionError",
    # Tool contracts stay in core (C2); the concrete InMemoryToolManager
    # lives in tools.manager and must NOT be re-exported here.
    "Tool", "ToolManager", "ToolConfig", "ToolResult",
    "ToolExecutionContext", "get_tool_execution_context",
    "ExecutionMode", "ParallelTool", "ExclusiveTool",
]


def test_core_exports_public_surface() -> None:
    missing = [n for n in CORE_PUBLIC if not hasattr(core, n)]
    assert not missing, f"core facade missing: {missing}"
    assert set(CORE_PUBLIC).issubset(set(core.__all__))
