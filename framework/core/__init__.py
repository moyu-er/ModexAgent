"""框架核心 - 基础类型和抽象基类"""

from .agent import (
    Agent,
    AgentContext,
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
    EphemeralContextManager,
    InMemoryContextManager,
)
from .emitter import (
    AgentResult,
    BufferingEmitter,
    ContentEmitter,
    LoggingEmitter,
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
    FunctionalTool,
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolExecutionMode,
    ToolManager,
    ToolManagerConfig,
    ToolResult,
)
from .types import (
    InputMessage,
    MessageRole,
    MessageType,
    OutputMessage,
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
    # 类型
    "MessageType",
    "InputMessage",
    "OutputMessage",
    "ToolCall",
    "ToolResult",
    # V2 新架构 - 事件和配置
    "AgentEvent",
    "EmitterConfig",
    # V2 新架构 - Emitter
    "AgentResult",
    "ContentEmitter",
    "BufferingEmitter",
    "LoggingEmitter",
    # V2 新架构 - 工具管理
    "ToolManager",
    "InMemoryToolManager",
    "Tool",
    "FunctionalTool",
    "ToolExecutionMode",
    "ToolConfig",
    "ToolManagerConfig",
    # V2 新架构 - Agent
    "Agent",
    "AgentContext",
    # V2 新架构 - 上下文管理
    "ContextManager",
    "InMemoryContextManager",
    "EphemeralContextManager",
    "ContextState",
    # Tool Call Accumulator
    "ToolCallAccumulator",
    "ToolCallChunk",
    "parse_tool_call_chunks_from_delta",
    # 抽象基类
    "LLMProvider",
    "StreamingLLMProvider",
]
