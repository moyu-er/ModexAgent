"""基础类型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import TYPE_CHECKING, Any

from .constants import DefaultValues
from .session_id import SessionInfo

if TYPE_CHECKING:
    from .llm_struct import LLMErrorInfo


class MessageType(Enum):
    """消息类型"""

    TEXT = "text"
    COMMAND = "command"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    ERROR = "error"


class MessageRole(StrEnum):
    """LLM 对话消息中的 role 字段枚举。

    用于替代硬编码的字符串，确保类型安全。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    AGENT = "agent"
    PENDING = "pending"


# ============================================================================
# 消息类型（V2 架构统一）
# ============================================================================


@dataclass
class InputMessage:
    """标准化的输入消息（V2 架构）

    用于 InputAdapter 接收的消息，替代旧的 InboundMessage。

    字段说明：
    - content: 消息内容（唯一必填字段）
    - session: 会话ID（用于区分不同对话）
    - channel: 消息渠道（qq, cli, http, webhook 等）
    - sender_id: 发送者ID
    - chat_id: 聊天/群组ID
    - source: 来源标识（与 channel 类似，用于兼容性）
    - msg_type: 消息类型
    - metadata: 额外元数据
    - attachments: 附件本地文件路径列表（图片、文档等）
    - timestamp: 时间戳
    """

    content: str  # 消息内容（唯一必填字段）
    session: SessionInfo
    channel: str = field(default=DefaultValues.CHANNEL)
    sender_id: str = field(default=DefaultValues.SENDER_ID)
    chat_id: str = field(default=DefaultValues.CHAT_ID)
    source: str = "unknown"  # 来源标识（与 channel 类似）
    msg_type: MessageType = field(default=MessageType.TEXT)
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    content_format: Any | None = None
    truncatable_paths: list[str] | None = None


@dataclass
class OutputMessage:
    """标准化的输出消息（V2 架构）

    用于 OutputAdapter 发送的消息，替代旧的 OutboundMessage。

    字段说明：
    - content: 消息内容（唯一必填字段）
    - session_id: 会话ID
    - channel: 消息渠道
    - recipient_id: 接收者ID
    - chat_id: 聊天/群组ID
    - message_type: 消息类型（text, image, file, error）
    - msg_type: 消息类型枚举（与 message_type 互补）
    - reasoning: 推理/思考过程（DeepSeek R1, Kimi 等模型）
    - metadata: 额外元数据
    - attachments: 附件本地文件路径列表（图片、文档等）
    - timestamp: 时间戳
    """

    content: str  # 消息内容（唯一必填字段）
    session_id: str = "default"
    channel: str = field(default=DefaultValues.CHANNEL)
    recipient_id: str = field(default=DefaultValues.RECIPIENT_ID)
    chat_id: str = field(default=DefaultValues.CHAT_ID)
    message_type: str = "text"  # text, image, file, error
    msg_type: MessageType = field(default=MessageType.TEXT)
    reasoning: str | None = None  # 推理/思考过程（DeepSeek R1, Kimi 等模型）
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ToolCall:
    """工具调用请求"""

    tool_name: str
    arguments: dict[str, Any]
    call_id: str | None = None


# Note: ToolResult is now defined in tool_manager.py
# Import from framework.core.tool_manager instead


@dataclass
class LLMResponse:
    """LLM 统一响应结构

    流式与非流式共享同一数据结构，Agent 解析逻辑只写一次。
    """

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str | None = None
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    error_info: LLMErrorInfo | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
