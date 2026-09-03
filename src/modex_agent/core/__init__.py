"""框架核心 - 基础类型和抽象基类"""

from .agent import (
    Agent,
    AgentCommKind,
    AgentContext,
    AgentRole,
    ExecutionStrategyKind,
    ProviderKind,
    current_agent_context,
)
from .capabilities import Modality, ModelCapabilities, ModelInfo
from .emitter import (
    AgentResult,
    ContentEmitter,
    StopReason,
)

# V2 新架构 - 核心抽象层
from .events import (
    AgentEvent,
    EmitterConfig,
)
from .history import MessageHistory
from .llm_request import LLMRequest, ReasoningEffort
from .llm_struct import FinishReason, LLMResponse, RuntimeSafetyPolicy, TokenUsage
from .media import (
    Attachment,
    AttachmentLocator,
    Kind,
    MediaRefCollisionError,
    MediaStore,
    StoredFile,
    StoredMediaKind,
)
from .message import (
    ChatMessage,
    ContentFormat,
    MessageRole,
    ToolCall,
)
from .prompt import SystemPromptPipeline
from .provider import CallbackStreamProvider, LLMProvider
from .session_id import (
    SessionIdFactory,
    SessionInfo,
    agent_of,
    encode_snowflake,
    session_id_prefix_of,
)
from .stream_events import (
    EventAssembler,
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    ReplayFields,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from .tool_manager import (
    ExclusiveTool,
    ExecutionMode,
    ParallelTool,
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolResult,
    get_tool_execution_context,
)
from .turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

__all__ = [
    "MessageRole",
    "FinishReason",
    "StopReason",
    # 类型
    "ToolCall",
    "ToolResult",
    "TurnEvent",
    "TurnReasoningEvent",
    "TurnTextEvent",
    "TurnToolCallEvent",
    "TurnToolResultEvent",
    # LLM 流式事件 (LLMStreamEvent 封闭联合)
    "EventAssembler",
    "ReplayFields",
    "TextDelta",
    "ReasoningDelta",
    "ToolCallComplete",
    "UsageSnapshot",
    "Finish",
    "StreamFailure",
    "LLMStreamEvent",
    # V2 新架构 - 事件和配置
    "AgentEvent",
    "EmitterConfig",
    # V2 新架构 - Emitter
    "AgentResult",
    "ContentEmitter",
    # V2 新架构 - 工具管理
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
    # V2 新架构 - Agent
    "Agent",
    "AgentCommKind",
    "AgentContext",
    "AgentRole",
    "ExecutionStrategyKind",
    "ProviderKind",
    # 抽象基类
    "MessageHistory",
    "LLMProvider",
    "CallbackStreamProvider",
    # Agent - 当前上下文
    "current_agent_context",
    # 会话 ID
    "SessionInfo",
    "SessionIdFactory",
    "agent_of",
    "session_id_prefix_of",
    "encode_snowflake",
    # 消息
    "ChatMessage",
    "ContentFormat",
    # 类型扩展
    "LLMRequest",
    "LLMResponse",
    "ReasoningEffort",
    "TokenUsage",
    # 运行时结构
    "RuntimeSafetyPolicy",
    "SystemPromptPipeline",
    # 媒体契约 (C1, ADR-0013)
    "Attachment",
    "AttachmentLocator",
    "Kind",
    "MediaRefCollisionError",
    "MediaStore",
    "StoredFile",
    "StoredMediaKind",
]
