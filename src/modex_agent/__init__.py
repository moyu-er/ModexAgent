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

from ._version import __version__  # noqa: I001 - messaging must load before adapters
from .messaging import InputMessage, MessageType, OutputMessage
from .adapters import BufferingSink, OutputAdapter
from .agents.react import ReActAgent
from .core import (
    Agent,
    AgentContext,
    AgentResult,
    CallbackStreamProvider,
    CompositeTurnEventSink,
    KindGate,
    LLMProvider,
    Tool,
    ToolCall,
    ToolConfig,
    ToolManager,
    ToolResult,
    TurnBinding,
    TurnEvent,
    TurnEventSink,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from .memory import ContextManager, ContextState
from .pipeline import AgentPipeline, InputAdapter
from .tools import InMemoryToolManager

__all__ = [
    "__version__",
    # 基础类型
    "MessageType",
    "ToolCall",
    "ToolResult",
    # 抽象基类
    "Tool",
    "LLMProvider",
    "CallbackStreamProvider",
    # 事件和 sink
    "TurnEvent",
    "TurnReasoningEvent",
    "TurnTextEvent",
    "TurnToolCallEvent",
    "TurnToolResultEvent",
    "TurnEventSink",
    "CompositeTurnEventSink",
    "KindGate",
    "TurnBinding",
    # Emitter
    "AgentResult",
    "BufferingSink",
    # 工具管理
    "ToolManager",
    "InMemoryToolManager",
    "ToolConfig",
    # Agent
    "Agent",
    "AgentContext",
    # 上下文管理
    "ContextManager",
    "ContextState",
    # Agent 实现
    "ReActAgent",
    # Pipeline
    "AgentPipeline",
    "InputAdapter",
    "OutputAdapter",
    "InputMessage",
    "OutputMessage",
]
