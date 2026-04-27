"""LLM 错误类型、安全策略配置与辅助函数。

定义跨 Provider/Agent/Pipeline 层使用的结构化错误类型和超时/熔断策略，
避免硬编码字符串散落在各模块中。
"""

from dataclasses import dataclass, field
from enum import StrEnum

from .constants import DefaultValues


# ─── 错误分类 ──────────────────────────────────────────────────────────────────


class LLMErrorKind(StrEnum):
    """LLM 错误类别，用于框架层统一的 retry/熔断/日志决策。"""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    QUOTA = "quota"
    CONNECTION = "connection"
    SERVER = "server"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class LLMErrorInfo:
    """结构化的 LLM 错误信息，附加在 LLMResponse 上供上层决策。"""

    kind: LLMErrorKind
    message: str
    provider: str | None = None
    status_code: int | None = None
    retry_after_seconds: float | None = None
    should_retry: bool = False


def classify_litellm_error(exc: Exception) -> LLMErrorInfo:
    """将 LiteLLM / httpx / aiohttp 异常归类为 LLMErrorInfo。

    分类以字符串扫描为主，保持保守：明确不可重试的先判定，
    其余标记为 UNKNOWN / should_retry=False。
    """
    text = str(exc).lower()
    cls_name = type(exc).__name__.lower()
    message = str(exc)[:500]

    if "timeout" in text or "timeout" in cls_name or "timed out" in text:
        return LLMErrorInfo(LLMErrorKind.TIMEOUT, message, "litellm", should_retry=True)

    if "rate" in text and "limit" in text:
        if any(m in text for m in ("quota", "billing", "insufficient")):
            return LLMErrorInfo(LLMErrorKind.QUOTA, message, "litellm", should_retry=False)
        return LLMErrorInfo(LLMErrorKind.RATE_LIMIT, message, "litellm", should_retry=True)

    if "auth" in text or "401" in text or "403" in text:
        return LLMErrorInfo(LLMErrorKind.AUTH, message, "litellm", should_retry=False)

    if any(code in text for code in ("500", "502", "503", "504")):
        return LLMErrorInfo(LLMErrorKind.SERVER, message, "litellm", should_retry=True)

    if "connection" in text or "connect" in cls_name:
        return LLMErrorInfo(LLMErrorKind.CONNECTION, message, "litellm", should_retry=True)

    return LLMErrorInfo(LLMErrorKind.UNKNOWN, message, "litellm", should_retry=False)


# ─── Provider 标识 ─────────────────────────────────────────────────────────────


class ProviderKind(StrEnum):
    """LLM Provider 种类。"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LITELLM = "litellm"


# ─── 安全策略配置 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LLMTimeoutPolicy:
    """单次 LLM 调用的超时与重试策略。"""

    request_timeout_seconds: float = 45.0
    stream_idle_timeout_seconds: float = 90.0
    framework_max_retries: int = 1
    retry_backoff_seconds: tuple[float, ...] = (2.0, 8.0)


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    """熔断器配置。"""

    enabled: bool = True
    failure_threshold: int = 3
    cooldown_seconds: float = 120.0


@dataclass(frozen=True)
class TurnTimeoutPolicy:
    """单个 Turn 各阶段超时配置。"""

    agent_run_timeout_seconds: float = 180.0
    dispatch_timeout_seconds: float = 300.0
    output_send_timeout_seconds: float = 20.0
    memory_flush_timeout_seconds: float = 30.0
    hook_timeout_seconds: float = 10.0
    tool_timeout_seconds: float = DefaultValues.TOOL_TIMEOUT_SECONDS


@dataclass(frozen=True)
class RuntimeSafetyPolicy:
    """P0/P1 运行时安全策略聚合。

    注入路径：AgentPipeline.__init__ / AgentPool，放入 context metadata 或
    显式字段，供 ReActAgent / emitter / hook / memory 各层读取。
    """

    llm: LLMTimeoutPolicy = field(default_factory=LLMTimeoutPolicy)
    circuit_breaker: CircuitBreakerPolicy = field(default_factory=CircuitBreakerPolicy)
    turn: TurnTimeoutPolicy = field(default_factory=TurnTimeoutPolicy)


@dataclass(frozen=True)
class LLMProviderConfig:
    """LLM Provider 最小配置结构，用于工厂创建。"""

    provider: ProviderKind = ProviderKind.OPENAI
    model: str = ""
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = DefaultValues.TEMPERATURE
    max_tokens: int | None = None
    timeout: float = DefaultValues.TIMEOUT_SECONDS
    stream_idle_timeout: float = 90.0
    parse_think_tags: bool = False
    reasoning_effort: str | None = None
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
        finish_reason="error",
        error=message,
        error_info=LLMErrorInfo(
            kind=LLMErrorKind.TIMEOUT,
            message=message,
            provider=provider,
            should_retry=True,
        ),
    )
