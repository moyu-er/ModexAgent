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

    Shared by ``classify_litellm_error`` and ``error_recovery.is_context_overflow_error``
    so the marker vocabulary stays in one place. Context-overflow errors are never
    retryable — the fix is compaction, not re-sending the same payload.
    """
    import re

    return any(
        re.search(marker, text) if ".*" in marker else marker in text
        for marker in _CONTEXT_OVERFLOW_MARKERS
    )


def is_content_filter_text(text: str) -> bool:
    """Return True if a lowercased error text indicates content moderation.

    Used by both the openai and litellm classifiers so the marker vocabulary
    stays in one place. Content-moderation errors are never retryable.
    """
    return any(marker in text for marker in _CONTENT_FILTER_MARKERS)


def classify_litellm_error(exc: Exception) -> LLMErrorInfo:
    """将 LiteLLM / httpx / aiohttp 异常归类为 LLMErrorInfo。

    分类以字符串扫描为主，保持保守：明确不可重试的先判定，
    其余标记为 UNKNOWN / should_retry=False。
    """
    text = str(exc).lower()
    cls_name = type(exc).__name__.lower()
    message = str(exc)[:500]

    if is_content_filter_text(text):
        return LLMErrorInfo(
            kind=LLMErrorKind.CONTENT_FILTER, message=message, provider="litellm", should_retry=False
        )

    if is_context_overflow_text(text):
        return LLMErrorInfo(
            kind=LLMErrorKind.INVALID_REQUEST,
            message=message,
            provider="litellm",
            should_retry=False,
        )

    if "timeout" in text or "timeout" in cls_name or "timed out" in text:
        return LLMErrorInfo(kind=LLMErrorKind.TIMEOUT, message=message, provider="litellm", should_retry=True)

    if "rate" in text and "limit" in text:
        if any(m in text for m in ("quota", "billing", "insufficient")):
            return LLMErrorInfo(kind=LLMErrorKind.QUOTA, message=message, provider="litellm", should_retry=False)
        return LLMErrorInfo(kind=LLMErrorKind.RATE_LIMIT, message=message, provider="litellm", should_retry=True)

    if "auth" in text or "401" in text or "403" in text:
        return LLMErrorInfo(kind=LLMErrorKind.AUTH, message=message, provider="litellm", should_retry=False)

    if any(code in text for code in ("500", "502", "503", "504")):
        return LLMErrorInfo(kind=LLMErrorKind.SERVER, message=message, provider="litellm", should_retry=True)

    if "connection" in text or "connect" in cls_name:
        return LLMErrorInfo(kind=LLMErrorKind.CONNECTION, message=message, provider="litellm", should_retry=True)

    return LLMErrorInfo(kind=LLMErrorKind.UNKNOWN, message=message, provider="litellm", should_retry=False)


# ─── Provider 标识 ─────────────────────────────────────────────────────────────


class LLMProviderKind(StrEnum):
    """LLM Provider 种类。

    Renamed from ``ProviderKind`` to disambiguate from the coding-agent
    ``ProviderKind`` in ``modex_agent.core.constants`` (PI / OPENCODE).
    This enum is LLM-provider-only (OpenAI / Anthropic / LiteLLM) and is
    used solely by ``LLMProviderConfig`` below.
    """

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LITELLM = "litellm"


# ─── 安全策略配置 ──────────────────────────────────────────────────────────────


class LLMTimeoutPolicy(BaseModel):
    """单次 LLM 调用的超时与重试策略。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_timeout_seconds: float = 45.0
    stream_idle_timeout_seconds: float = 90.0
    framework_max_retries: int = 1
    retry_backoff_seconds: tuple[float, ...] = (2.0, 8.0)


class TurnTimeoutPolicy(BaseModel):
    """单个 Turn 各阶段超时配置。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_run_timeout_seconds: float = 420.0
    dispatch_timeout_seconds: float = 300.0
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
    """LLM Provider 最小配置结构，用于工厂创建。"""

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
