"""框架级常量定义

集中管理所有硬编码字符串、枚举值和默认配置，避免分散在各个模块中。
"""

from enum import Enum, StrEnum


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
    ERROR = "error"
    CANCELLED = "cancelled"


class StopReason(StrEnum):
    """Agent turn 结束原因枚举。

    统一所有 turn-level 的停止原因，消除分散在各处的硬编码字符串。
    """

    COMPLETED = "completed"
    ERROR = "error"
    MAX_ITERATIONS = "max_iterations"
    TURN_CANCELLED = "turn_cancelled"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MISSED_COMMUNICATION = "missed_communication"
    COMMAND_INTERCEPTED = "command_intercepted"
    DUPLICATE = "duplicate"
    LOOP_DETECTED = "loop_detected"


class ExecutionStrategyKind(StrEnum):
    """Agent 执行策略。

    与 ``AgentDescriptor.execution_strategy`` 对应，集中管理所有合法的
    执行策略值，避免分散在 descriptor / pool config / 业务层中的硬编码字符串。

    Renamed from ``ExecutionStrategy`` so the bare name refers exclusively to
    the ABC in ``modex_agent.multi_agent.execution_strategy`` (ADR-0025).
    Pool.yml string values (``react`` / ``external_coding`` / ...) are
    unchanged — this is a Python-symbol-only rename.
    """

    REACT = "react"
    SINGLE_TURN = "single_turn"
    PIPELINE = "pipeline"
    EXTERNAL_CODING = "external_coding"


class ReasoningEffort(StrEnum):
    """模型 reasoning effort 参数的可选值。

    OpenAI 兼容 API 用于控制模型内部推理量的参数。``NONE`` 表示不发送该
    参数，保持与未配置时一致的行为。
    """

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class InterfaceFormat(StrEnum):
    """LLM 接口格式。

    决定后端使用哪个 provider 以及是否需要在模型名前补充 provider 前缀。
    """

    OPENAI_COMPATIBLE = "openai_compatible"
    ANTHROPIC = "anthropic"


class AgentRole(StrEnum):
    """Preset agent role constants.

    Bots may extend with custom string roles outside this enum; the
    ``roles`` field on ``MainAgentSpec`` / ``SubagentSpec`` /
    ``AgentDescriptor`` is ``list[str]`` (not ``list[AgentRole]``) so custom
    values are preserved verbatim. The enum exists to centralize the seven
    canonical preset names (rule 1, rule 14) — string literals elsewhere
    should reference ``AgentRole.PLANNER.value`` etc. instead of bare
    ``"planner"``.
    """

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    SCOUT = "scout"
    ORACLE = "oracle"
    COORDINATOR = "coordinator"
    COMMUNICATOR = "communicator"


class ErrorMessages:
    """错误消息常量"""

    NO_TOOL_REGISTRY = "没有可用的工具注册表"
    MAX_TOOL_CALLS_EXCEEDED = "达到最大工具调用次数限制"
    MAX_ITERATIONS_EXCEEDED = "达到最大迭代次数限制"
    TOOL_NOT_FOUND = "Tool '{name}' not found"
    TOOL_DISABLED = "Tool '{name}' is disabled"
    REGISTRY_NOT_SET = "ToolManager not set. Call set_registries() first."
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
    TOOL_TIMEOUT_SECONDS = 360.0
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
