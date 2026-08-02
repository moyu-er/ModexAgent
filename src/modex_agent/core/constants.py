"""框架级常量定义

集中管理所有硬编码字符串、枚举值和默认配置，避免分散在各个模块中。
"""

from enum import Enum, StrEnum
from pathlib import Path


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


class StreamControlAction(StrEnum):
    """LLM 流式控制动作枚举。

    用于 ``LLMStreamChunk.control_action``，标识流式过程中由控制层
    注入的动作（如取消当前流）。``None`` 表示无控制动作。
    """

    CANCEL = "cancel"


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
    Pool.yml string values (``react`` / ``external`` / ...) are
    unchanged — this is a Python-symbol-only rename.
    """

    REACT = "react"
    SINGLE_TURN = "single_turn"
    PIPELINE = "pipeline"
    EXTERNAL = "external"


class ProviderKind(StrEnum):
    """Coding-agent provider families supported by ``ExternalAgent``.

    Day-one values are ``PI`` and ``OPENCODE``. Add a new value when a new
    CLI family (Claude Code, Codex, Cursor) is integrated — paired with a
    new ``ProviderBackend`` subclass and a new ``ProviderEventParser``.

    Co-located with ``ExecutionStrategyKind`` because the two are paired in
    pool-config validation (``_validate_execution_provider_pair`` in
    ``multi_agent.pool_config.specs``): ``provider_kind`` must be set iff
    ``execution_strategy`` is ``EXTERNAL``. Keeping them in the same leaf
    module lets data-layer types (``AgentDescriptor``, ``MainAgentSpec``,
    ``SubagentSpec``) depend on ``core.constants`` without pulling in
    ``agents.external.paths`` (which would close a cycle through
    ``agents.external.__init__``).
    """

    PI = "pi"
    OPENCODE = "opencode"


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
    TOOL_TIMEOUT_SECONDS = 120.0
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


class RuntimeInfoKey(StrEnum):
    """Canonical keys for the ``runtime_info`` dict passed to ``ContextManager.load``."""

    CALLER_CONTEXT = "caller_context"
    MESSAGE = "message"
    PARENT_SESSION_ID = "parent_session_id"
    MODEL_INFO = "model_info"
    USER_ID = "user_id"
    TENANT_ID = "tenant_id"
    CHANNEL = "channel"
    CHAT_ID = "chat_id"
    WORKING_DIRECTORY = "working_directory"


# Sentinel for the RuntimeProvider version key when no working directory is bound.
# Included in the hourly version hash so the cache correctly distinguishes
# "no directory" from a specific directory within the same hour.
_NO_DIR_SENTINEL: str = "no-dir"


def format_working_directory_line(working_directory: str | Path | None) -> str | None:
    """Format the working-directory line for the system prompt runtime section.

    Returns ``None`` when *working_directory* is ``None`` so callers can skip
    the line entirely (preserving the "omit when absent" contract).
    """
    if working_directory is None:
        return None
    return (
        f"Working Directory: {working_directory}\n"
        "Use this directory as the base for relative file paths and shell commands."
    )
