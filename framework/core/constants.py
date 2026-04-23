"""框架级常量定义

集中管理所有硬编码字符串、枚举值和默认配置，避免分散在各个模块中。
"""

from enum import Enum


class MessageRole(str, Enum):
    """消息角色"""
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    FUNCTION = "function"


class ToolCallType(str, Enum):
    """工具调用类型"""
    FUNCTION = "function"


class ToolChoice(str, Enum):
    """工具选择策略"""
    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


class FinishReason(str, Enum):
    """LLM 响应完成原因"""
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"


class ErrorMessages:
    """错误消息常量"""
    NO_TOOL_REGISTRY = "没有可用的工具注册表"
    MAX_TOOL_CALLS_EXCEEDED = "达到最大工具调用次数限制"
    MAX_ITERATIONS_EXCEEDED = "达到最大迭代次数限制"
    TOOL_NOT_FOUND = "Tool '{name}' not found"
    TOOL_DISABLED = "Tool '{name}' is disabled"
    REGISTRY_NOT_SET = "ToolRegistry not set. Call set_registries() first."
    NO_REGISTRY_PROVIDED = "No registry provided"
    TOOL_NOT_IN_REGISTRY = "Tool '{name}' not found in registry"
    INVALID_ARGUMENTS_JSON = "Invalid arguments JSON: {args}"
    TOOL_NOT_FOUND_IN_TOOLKIT = "Tool '{name}' not found"


class DefaultValues:
    """默认值常量"""
    SYSTEM_PROMPT = "You are a helpful AI assistant. You have access to the conversation history and can remember previous messages in the current session."
    TEMPERATURE = 0.7
    MAX_ITERATIONS = 10
    MAX_TOOL_CALLS = 10
    TIMEOUT_SECONDS = 60.0
    TOOL_TIMEOUT_SECONDS = 30.0
    TOOL_VERSION = "1.0"
    TOOL_CATEGORY = "general"
    CALL_ID_PREFIX = "call_"
    
    # 消息相关默认值
    CHANNEL = "default"
    SENDER_ID = "anonymous"
    RECIPIENT_ID = "agent"
    CHAT_ID = "default_session"
    USER_ID = "default_user"
    AGENT_ID = "default_agent"


class ToolSchemaConstants:
    """工具 Schema 相关常量"""
    TYPE_FUNCTION = "function"
    PARAM_TYPE_OBJECT = "object"
