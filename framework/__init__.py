"""
Agent Framework Core - 多Agent服务框架核心

一个轻量级、可扩展的多Agent框架，支持：
- 多Channel消息总线
- Agent间通信
- ReAct执行循环
- 工具与技能系统
- 记忆与会话管理
- Adapter直接接入（无需Gateway）
"""

__version__ = "0.2.0"

from .agents import ReActAgent, ReActEvent
from .core.agent import Agent, AgentContext
from .core.context import ContextManager, ContextState
from .core.emitter import (
    AgentResult,
    ContentEmitter,
    StreamingAwareEmitter,
)
from .core.events import AgentEvent, EmitterConfig
from .core.provider import LLMProvider, StreamingLLMProvider
from .core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolExecutionMode,
    ToolManager,
    ToolManagerConfig,
    ToolResult,
)
from .core.types import (
    MessageType,
    ToolCall,
)
from .pipeline import (
    AgentPipeline,
    InputAdapter,
    InputMessage,
    OutputAdapter,
    OutputMessage,
)

__all__ = [
    "__version__",
    # 基础类型
    "MessageType",
    "ToolCall",
    "ToolResult",
    # 抽象基类
    "Tool",
    "LLMProvider",
    "StreamingLLMProvider",
    # 事件和配置
    "AgentEvent",
    "EmitterConfig",
    # Emitter
    "AgentResult",
    "ContentEmitter",
    "StreamingAwareEmitter",
    # 工具管理
    "ToolManager",
    "InMemoryToolManager",
    "ToolExecutionMode",
    "ToolConfig",
    "ToolManagerConfig",
    # Agent
    "Agent",
    "AgentContext",
    # 上下文管理
    "ContextManager",
    "ContextState",
    # Agent 实现
    "ReActAgent",
    "ReActEvent",
    # Pipeline
    "AgentPipeline",
    "InputAdapter",
    "OutputAdapter",
    "InputMessage",
    "OutputMessage",
]
