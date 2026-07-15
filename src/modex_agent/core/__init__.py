"""框架核心 - 基础类型和抽象基类"""

from .agent import (
    Agent,
    AgentCommKind,
    AgentContext,
    current_agent_context,
)
from .constants import (
    DefaultValues,
    ErrorMessages,
    FinishReason,
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
from .provider import LLMProvider, StreamingLLMProvider
from .tool_call_accumulator import (
    ToolCallAccumulator,
    ToolCallChunk,
    parse_tool_call_chunks_from_delta,
)
from .tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolManager,
    ToolManagerConfig,
    ToolResult,
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
from .turn_events import (
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from .session_id import (
    SessionInfo,
    SessionIdFactory,
    now_ms,
    agent_of,
    session_id_prefix_of,
    encode_snowflake,
)
from .session_store import (
    SessionStore,
    LocalFileSessionStore,
    safe_filename,
)
from .session_registry import (
    SessionRegistry,
    InMemorySessionRegistry,
)
from .message import (
    ChatMessage,
    ContentFormat,
)
from .llm_struct import RuntimeSafetyPolicy
from .runtime_context import RuntimeContextManager
from .prompt import SystemPromptPipeline
from .frontmatter import parse_frontmatter
from .utils import safe_atomic_replace

__all__ = [
    # 常量
    "MessageRole",
    "ToolCallType",
    "ToolChoice",
    "FinishReason",
    "ErrorMessages",
    "DefaultValues",
    "ToolSchemaConstants",
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
    "ToolManagerConfig",
    # V2 新架构 - Agent
    "Agent",
    "AgentCommKind",
    "AgentContext",
    # V2 新架构 - 上下文管理
    "ContextManager",
    "ContextState",
    # Tool Call Accumulator
    "ToolCallAccumulator",
    "ToolCallChunk",
    "parse_tool_call_chunks_from_delta",
    # 抽象基类
    "LLMProvider",
    "StreamingLLMProvider",
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
    "LLMResponse",
    "TodoStatus",
    # 运行时结构
    "RuntimeSafetyPolicy",
    "RuntimeContextManager",
    "SystemPromptPipeline",
    "parse_frontmatter",
    "safe_atomic_replace",
]
