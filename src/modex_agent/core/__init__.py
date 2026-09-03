"""框架核心 - 基础类型和抽象基类"""

from .agent import (
    Agent,
    AgentCommKind,
    AgentContext,
    current_agent_context,
)
from .capabilities import Modality, ModelCapabilities, ModelInfo
from .constants import (
    DefaultValues,
    ErrorMessages,
    FinishReason,
    RuntimeInfoKey,
    ToolCallType,
    ToolChoice,
    ToolSchemaConstants,
)
from .context import (
    ContextManager,
    ContextState,
)
from .emitter import (
    AgentResult,
    ContentEmitter,
)

# V2 新架构 - 核心抽象层
from .events import (
    AgentEvent,
    EmitterConfig,
)
from .llm_request import LLMRequest
from .llm_struct import RuntimeSafetyPolicy
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
)
from .prompt import SystemPromptPipeline
from .provider import CallbackStreamProvider, LLMProvider
from .session_id import (
    SessionIdFactory,
    SessionInfo,
    agent_of,
    encode_snowflake,
    now_ms,
    session_id_prefix_of,
)
from .session_registry import (
    InMemorySessionRegistry,
    SessionRegistry,
)
from .session_store import (
    LocalFileSessionStore,
    SessionStore,
    safe_filename,
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
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolExecutionContext,
    ToolManager,
    ToolManagerConfig,
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
from .types import (
    InputMessage,
    LLMResponse,
    MessageRole,
    MessageType,
    OutputMessage,
    TodoStatus,
    ToolCall,
)

__all__ = [
    # 常量
    "MessageRole",
    "ToolCallType",
    "ToolChoice",
    "FinishReason",
    "ErrorMessages",
    "DefaultValues",
    "ToolSchemaConstants",
    "RuntimeInfoKey",
    # 类型
    "MessageType",
    "InputMessage",
    "OutputMessage",
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
    "InMemoryToolManager",
    "Tool",
    "ToolConfig",
    "ToolExecutionContext",
    "ToolManagerConfig",
    "get_tool_execution_context",
    "Modality",
    "ModelCapabilities",
    "ModelInfo",
    # V2 新架构 - Agent
    "Agent",
    "AgentCommKind",
    "AgentContext",
    # V2 新架构 - 上下文管理
    "ContextManager",
    "ContextState",
    # 抽象基类
    "LLMProvider",
    "CallbackStreamProvider",
    # Agent - 当前上下文
    "current_agent_context",
    # 会话 ID
    "SessionInfo",
    "SessionIdFactory",
    "now_ms",
    "agent_of",
    "session_id_prefix_of",
    "encode_snowflake",
    # 会话存储
    "SessionStore",
    "LocalFileSessionStore",
    "safe_filename",
    # 会话注册表
    "SessionRegistry",
    "InMemorySessionRegistry",
    # 消息
    "ChatMessage",
    "ContentFormat",
    # 类型扩展
    "LLMRequest",
    "LLMResponse",
    "TodoStatus",
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
