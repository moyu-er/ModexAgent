"""基础类型定义"""

from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .constants import DefaultValues, FinishReason
from .llm_struct import LLMErrorInfo
from .media import Attachment
from .session_id import SessionInfo

if TYPE_CHECKING:
    from modex_agent.approval.views import ApprovalDecisionInput
    from modex_agent.core.message import ContentFormat


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
    COMPACT = "compact"
    PENDING = "pending"
    SYSTEM_REMINDER = "system_reminder"


class ReminderKind(StrEnum):
    """Category of a system-reminder message.

    Stored as ``ChatMessage`` metadata (``reminder_kind`` extra field) to
    classify the source/channel of a framework-to-agent notification.
    Used by builders, strategies, and ``InboxFlushHook``.
    """

    AGENT_MESSAGE = "agent_message"
    PEER_MESSAGE = "peer_message"
    SUBAGENT_RESULT = "subagent_result"
    TODO_REORIENTATION = "todo_reorientation"


class TodoStatus(StrEnum):
    """Status of a todo item in a session task list."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OutputMessageType(StrEnum):
    """输出消息类型枚举。

    用于 ``OutputMessage.message_type``，替代硬编码的字符串
    （text / image / file / error / approval_request / command_response / busy_notice / notice）。
    """

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    ERROR = "error"
    APPROVAL_REQUEST = "approval_request"
    COMMAND_RESPONSE = "command_response"
    BUSY_NOTICE = "busy_notice"
    NOTICE = "notice"


class ToolCall(BaseModel):
    """工具调用请求"""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    arguments: dict[str, Any]
    call_id: str | None = None


# ============================================================================
# 消息类型（V2 架构统一）
# ============================================================================


class InputMessage(BaseModel):
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
    - approval_decision: IM/WebUI 审批决策（非指令）；None 表示普通消息。
      WebUI 在审批端点构造；IM 由 ApprovalStage 从 /approve·/deny 解析。
    - attachments_resolved: gate-accepted inbound Attachment records for THIS
      turn (ADR-0013 §3/§11), produced by the attachment ingest stage. Typed
      carriage from the input pipeline to turn-preprocessing: ``preprocess``
      reads name/mime/size/path here to inject the transient path reference
      the agent perceives (mechanism B). The records are metadata only — never
      bytes. Empty when no attachment was accepted; outbound attachments are
      not listed (produced by SendFileToUserTool, not the input pipeline).
    """

    # ContentFormat and ApprovalDecisionInput are under TYPE_CHECKING due to
    # circular imports (core.message -> core.types -> core.message, and
    # approval.__init__ -> approval.ui -> core.types -> approval.views).
    # Pydantic creates this model with __pydantic_complete__=False; the forward
    # references are resolved lazily via _ensure_complete() on first use, when
    # all modules are fully loaded.
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    content: str  # 消息内容（唯一必填字段）
    session: SessionInfo
    channel: str = DefaultValues.CHANNEL
    sender_id: str = DefaultValues.SENDER_ID
    chat_id: str = DefaultValues.CHAT_ID
    source: str = "unknown"  # 来源标识（与 channel 类似）
    msg_type: MessageType = MessageType.TEXT
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)
    content_format: ContentFormat | None = None
    truncatable_paths: list[str] | None = None
    workspace: Path | None = None
    approval_decision: ApprovalDecisionInput | None = None
    attachments_resolved: list[Attachment] = Field(default_factory=list)

    @classmethod
    def _ensure_complete(cls) -> None:
        """Lazily resolve ContentFormat and ApprovalDecisionInput forward refs.

        These types live in modules that import back from ``core.types``
        (core.message imports ToolCall; approval.ui imports OutputMessage),
        so they cannot be imported at module load time. At runtime — when the
        first InputMessage is constructed — all modules are fully loaded and
        the imports succeed. ``model_rebuild`` updates the schema in place;
        subsequent calls are a no-op (``__pydantic_complete__`` is True).
        """
        if not cls.__pydantic_complete__:
            from modex_agent.approval.views import ApprovalDecisionInput  # noqa: F401
            from modex_agent.core.message import ContentFormat  # noqa: F401

            g = globals()
            g["ContentFormat"] = ContentFormat
            g["ApprovalDecisionInput"] = ApprovalDecisionInput
            cls.model_rebuild()

    def __init__(self, /, **data: Any) -> None:
        type(self)._ensure_complete()
        super().__init__(**data)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for cross-process transport.

        ``workspace`` (Path) and ``approval_decision`` (frozen dataclass, not a
        BaseModel) require custom serialization — Pydantic's ``model_dump`` does
        not handle ``arbitrary_types_allowed`` fields in JSON mode. They are
        excluded from the bulk dump and added manually.
        """
        type(self)._ensure_complete()
        data = self.model_dump(mode="json", exclude={"workspace", "approval_decision"})
        if self.workspace is not None:
            data["workspace"] = str(self.workspace)
        if self.approval_decision is not None:
            data["approval_decision"] = self.approval_decision.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InputMessage:
        """Reconstruct from ``to_dict`` output."""
        cls._ensure_complete()
        data = dict(data)
        if data.get("workspace") is not None:
            data["workspace"] = Path(data["workspace"])
        if data.get("approval_decision") is not None:
            from modex_agent.approval.views import ApprovalDecisionInput

            data["approval_decision"] = ApprovalDecisionInput.from_dict(data["approval_decision"])
        return cls.model_validate(data)


class OutputMessage(BaseModel):
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
    - attachment_records: outbound Attachment records (ADR-0013 §3). Populated
      by SendFileToUserTool after it persists the record; the OutputAdapter
      reads this to emit an attachment-card delta carrying the attachment_id
      (``attachments`` is path-only and cannot build a download URL).
      Direction-agnostic: the renderer (WebUI/IM) picks inline/card/fallback
      from kind + whether the download succeeds.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str  # 消息内容（唯一必填字段）
    session_id: str = "default"
    channel: str = DefaultValues.CHANNEL
    recipient_id: str = DefaultValues.RECIPIENT_ID
    chat_id: str = DefaultValues.CHAT_ID
    message_type: OutputMessageType = OutputMessageType.TEXT  # text, image, file, error
    msg_type: MessageType = MessageType.TEXT
    reasoning: str | None = None  # 推理/思考过程（DeepSeek R1, Kimi 等模型）
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.now)
    attachment_records: list[Attachment] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict (Pydantic-native, all fields are BaseModel/enum/primitive)."""
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OutputMessage:
        """Reconstruct from ``to_dict`` output."""
        return cls.model_validate(data)


# Note: ToolResult is now defined in tool_manager.py
# Import from framework.core.tool_manager instead


class TokenUsage(BaseModel):
    """Typed LLM token usage.

    ``input_tokens`` counts UNcached prompt tokens only; ``reasoning_tokens``
    is informational (already included in ``output_tokens``).

    The ``mode="before"`` validator normalizes the three provider wire shapes
    (OpenAI chat ``prompt_tokens`` + ``prompt_tokens_details.cached_tokens``,
    Anthropic ``input_tokens``/``cache_read_input_tokens``/
    ``cache_creation_input_tokens``, OpenAI Responses ``input_tokens`` +
    ``input_tokens_details.cached_tokens``) plus the legacy cassette /
    DeepSeek key forms (``prompt_cache_hit_tokens``,
    ``cache_creation.ephemeral_*_input_tokens``). Unknown keys are dropped.
    Under the OpenAI/DeepSeek/Responses convention the wire input count
    INCLUDES cached tokens (both ``prompt_tokens`` and Responses
    ``input_tokens``), so ``input_tokens = wire_input - cache_read``; the
    Anthropic wire shape is already the uncached count and passes through.
    A negative result raises ``ValueError`` instead of silently producing a
    negative count. ``total_tokens`` is always recomputed from the four
    counters and never taken from the wire.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
            + self.output_tokens
        )

    @model_validator(mode="before")
    @classmethod
    def _normalize_wire_keys(cls, data: Any) -> Any:
        if isinstance(data, TokenUsage) or not isinstance(data, dict):
            return data
        raw: dict[str, Any] = dict(data)

        def read_int(key: str) -> int | None:
            value = raw.get(key)
            return None if value is None else int(value)

        def read_nested_int(parent: str, child: str) -> int | None:
            details = raw.get(parent)
            if not isinstance(details, dict):
                return None
            value = details.get(child)
            return None if value is None else int(value)

        cache_read = read_int("cache_read_input_tokens")
        if cache_read is None:
            cache_read = read_nested_int("prompt_tokens_details", "cached_tokens")
        if cache_read is None:
            cache_read = read_nested_int("input_tokens_details", "cached_tokens")
        if cache_read is None:
            cache_read = read_int("prompt_cache_hit_tokens")

        cache_creation = read_int("cache_creation_input_tokens")
        if cache_creation is None:
            ephemeral_5m = read_nested_int("cache_creation", "ephemeral_5m_input_tokens")
            ephemeral_1h = read_nested_int("cache_creation", "ephemeral_1h_input_tokens")
            if ephemeral_5m is not None or ephemeral_1h is not None:
                cache_creation = (ephemeral_5m or 0) + (ephemeral_1h or 0)

        reasoning = read_int("reasoning_tokens")
        if reasoning is None:
            reasoning = read_nested_int("completion_tokens_details", "reasoning_tokens")
        if reasoning is None:
            reasoning = read_nested_int("output_tokens_details", "reasoning_tokens")

        output = read_int("output_tokens")
        if output is None:
            output = read_int("completion_tokens")

        # One total-prompt-input number per wire shape, flagged with whether
        # it INCLUDES the cached portion. Anthropic reports the uncached
        # remainder directly; OpenAI chat (prompt_tokens) and Responses
        # (input_tokens) report cache-inclusive totals.
        total_input = read_int("input_tokens")
        input_includes_cache = (
            total_input is not None
            and read_nested_int("input_tokens_details", "cached_tokens") is not None
        )
        if total_input is None:
            prompt_tokens = read_int("prompt_tokens")
            total_input = prompt_tokens if prompt_tokens is not None else 0
            input_includes_cache = prompt_tokens is not None

        if cache_read and input_includes_cache:
            total_input -= cache_read

        input_tokens = total_input or 0

        normalized = {
            "input_tokens": input_tokens or 0,
            "cache_read_input_tokens": cache_read or 0,
            "cache_creation_input_tokens": cache_creation or 0,
            "output_tokens": output or 0,
            "reasoning_tokens": reasoning or 0,
        }
        negative = {key: value for key, value in normalized.items() if value < 0}
        if negative:
            raise ValueError(
                f"TokenUsage normalization produced negative token counts: {negative}; "
                f"input data: {data!r}"
            )
        return normalized


class LLMResponse(BaseModel):
    """LLM 统一响应结构

    流式与非流式共享同一数据结构，Agent 解析逻辑只写一次。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    reasoning_item_id: str | None = None
    reasoning_encrypted_content: str | None = None
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage = Field(default_factory=TokenUsage)
    completion_start_time: str | None = None
    response_id: str | None = None
    error: str | None = None
    error_info: LLMErrorInfo | None = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
