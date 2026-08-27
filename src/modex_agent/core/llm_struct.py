"""LLM 错误类型、安全策略配置与辅助函数。

定义跨 Provider/Agent/Pipeline 层使用的结构化错误类型和超时策略，
避免硬编码字符串散落在各模块中。
"""

import logging
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from .constants import DefaultValues, FinishReason, ReasoningEffort

if TYPE_CHECKING:
    from .types import LLMResponse

logger = logging.getLogger(__name__)


# ─── 错误分类 ──────────────────────────────────────────────────────────────────


class LLMErrorKind(StrEnum):
    """LLM 错误类别，用于框架层统一的 retry/日志决策。"""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    QUOTA = "quota"
    CONNECTION = "connection"
    SERVER = "server"
    INVALID_REQUEST = "invalid_request"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


class LLMErrorInfo(BaseModel):
    """结构化的 LLM 错误信息，附加在 LLMResponse 上供上层决策。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: LLMErrorKind
    message: str
    provider: str | None = None
    status_code: int | None = None
    retry_after_seconds: float | None = None
    should_retry: bool = False


_CONTENT_FILTER_MARKERS = (
    "content_filter",
    "content filter",
    "content management policy",
    "content policy",
    "sensitive",  # GLM/Zhipu moderation code, e.g. "new_sensitive (1027)"
)

# Context-overflow errors: the prompt exceeds the model's context window.
# NEVER retry — re-sending the same oversized payload loops forever, burning
# tokens. Must be matched before the generic SERVER/timeout checks so that a
# 400-status "context length exceeded" response is not misclassified as
# retryable. The recovery path is session compaction, not retry.
_CONTEXT_OVERFLOW_MARKERS = (
    "413",
    "context length",
    "context_length",
    "maximum context",
    "payload too large",
    "too long",
    "token limit",
    "reduce the length",
    "prompt is too long",
    "exceeds.*token",
    "input.*too long",
)


def is_context_overflow_text(text: str) -> bool:
    """Return True if a lowercased error text indicates a context-window overflow.

    Shared by the direct-HTTP error classifier (providers/http/errors.py)
    and ``error_recovery.is_context_overflow_error`` so the marker
    vocabulary stays in one place. Context-overflow errors are never
    retryable — the fix is compaction, not re-sending the same payload.
    """
    import re

    return any(
        re.search(marker, text) if ".*" in marker else marker in text
        for marker in _CONTEXT_OVERFLOW_MARKERS
    )


def is_content_filter_text(text: str) -> bool:
    """Return True if a lowercased error text indicates content moderation.

    Used by the direct-HTTP protocol engines' error classification so the
    marker vocabulary stays in one place. Content-moderation errors are
    never retryable.
    """
    return any(marker in text for marker in _CONTENT_FILTER_MARKERS)


# ─── Provider 标识 ─────────────────────────────────────────────────────────────


class LLMProviderKind(StrEnum):
    """LLM Provider 种类。

    Renamed from ``ProviderKind`` to disambiguate from the coding-agent
    ``ProviderKind`` in ``modex_agent.core.constants`` (PI / OPENCODE).
    This enum is LLM-provider-only (OpenAI / Anthropic) and is used solely
    by ``LLMProviderConfig`` below.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"


# ─── 安全策略配置 ──────────────────────────────────────────────────────────────


class LLMTimeoutPolicy(BaseModel):
    """单次 LLM 调用的超时与重试策略。

    默认不设置 provider 层超时（None = 无限等待），依赖外层 turn timeout
    + watchdog 作为唯一终止机制。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_timeout_seconds: float | None = None
    stream_idle_timeout_seconds: float | None = None
    framework_max_retries: int = 2
    retry_backoff_seconds: tuple[float, ...] = (2.0, 8.0)


class TurnTimeoutPolicy(BaseModel):
    """单个 Turn 各阶段超时配置。

    ``agent_run_timeout_seconds`` is the per-iteration renewal amount passed
    to ``DispatchDeadline.renew()`` after each LLM iteration (nodes/llm.py).
    It is NOT a hard ceiling on total turn duration — the sliding ceiling
    (``DispatchDeadline.max_ahead_seconds``) governs that. See
    ``runtime/dispatch.py`` for the full timeout architecture.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_run_timeout_seconds: float = 600.0
    dispatch_timeout_seconds: float = 600.0
    output_send_timeout_seconds: float = 20.0
    memory_flush_timeout_seconds: float = 30.0
    hook_timeout_seconds: float = 10.0
    tool_timeout_seconds: float = DefaultValues.TOOL_TIMEOUT_SECONDS


class RuntimeSafetyPolicy(BaseModel):
    """P0/P1 运行时安全策略聚合。

    注入路径：AgentPipeline.__init__ / AgentPool，放入 context metadata 或
    显式字段，供 ReActAgent / emitter / hook / memory 各层读取。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    llm: LLMTimeoutPolicy = Field(default_factory=LLMTimeoutPolicy)
    turn: TurnTimeoutPolicy = Field(default_factory=TurnTimeoutPolicy)


class LLMProviderConfig(BaseModel):
    """LLM Provider 最小配置结构，用于工厂创建。

    休眠（dormant）配置：新代码勿用，LLM 接入走 ``ioc/configs/llm.py`` 的
    ``LLMConfig``。本结构不删不改字段保留——其 ``extra_headers`` 即 headers
    透传的前身证据。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: LLMProviderKind = LLMProviderKind.OPENAI
    model: str = ""
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = DefaultValues.TEMPERATURE
    max_output_tokens: int | None = None
    timeout: float = DefaultValues.TIMEOUT_SECONDS
    stream_idle_timeout: float = 90.0
    parse_think_tags: bool = True
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    extra_headers: dict[str, str] | None = None


# ─── 辅助构造 ──────────────────────────────────────────────────────────────────


def build_timeout_response(
    *,
    provider: str,
    message: str,
    partial_content: str = "",
) -> "LLMResponse":
    """构建 stream idle timeout 的统一 error response。

    content 可保留 partial content 供日志/调试；
    上层 ReActAgent 必须依赖 finish_reason==ERROR 判定失败。
    """
    # 延迟导入避免循环
    from .types import LLMResponse

    return LLMResponse(
        content=partial_content or message,
        finish_reason=FinishReason.ERROR,
        error=message,
        error_info=LLMErrorInfo(
            kind=LLMErrorKind.TIMEOUT,
            message=message,
            provider=provider,
            should_retry=True,
        ),
    )
