"""LLM 错误类型、安全策略配置与辅助函数。

定义跨 Provider/Agent/Pipeline 层使用的结构化错误类型和超时/熔断策略，
避免硬编码字符串散落在各模块中。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .constants import DefaultValues

if TYPE_CHECKING:
    from .types import LLMResponse

logger = logging.getLogger(__name__)


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
    CIRCUIT_BREAKER = "circuit_breaker"


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


# ─── 熔断器 ────────────────────────────────────────────────────────────────────


class CircuitBreakerState(StrEnum):
    """熔断器三态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """基于失败次数和冷却时间的熔断器。

    三态状态机:
      CLOSED   --failure_threshold failures--> OPEN
      OPEN     --cooldown expires--> HALF_OPEN
      HALF_OPEN--1 success--> CLOSED
      HALF_OPEN--1 failure--> OPEN
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 3,
        cooldown_seconds: float = 120.0,
        enabled: bool = True,
    ):
        self._name = name
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown_seconds = max(0.0, cooldown_seconds)
        self._enabled = enabled
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_open(self) -> bool:
        """当前是否处于熔断状态（含 HALF_OPEN 的一次试探）。"""
        if not self._enabled:
            return False
        if self._state == CircuitBreakerState.OPEN:
            return not self._cooldown_has_expired()
        return False

    def is_half_open(self) -> bool:
        return self._state == CircuitBreakerState.HALF_OPEN

    def _cooldown_has_expired(self) -> bool:
        if self._last_failure_time is None:
            return True
        return (time.monotonic() - self._last_failure_time) >= self._cooldown_seconds

    async def record(self, error_info: LLMErrorInfo) -> None:
        """根据错误信息记录失败（仅对 timeout/server/connection 累计）。"""
        if not self._enabled:
            return
        if error_info.kind not in (
            LLMErrorKind.TIMEOUT,
            LLMErrorKind.SERVER,
            LLMErrorKind.CONNECTION,
        ):
            return
        async with self._lock:
            await self._record_failure_locked()

    async def record_success(self) -> None:
        async with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                logger.info(
                    "Circuit breaker %s: half-open → closed after success",
                    self._name,
                )
                self._state = CircuitBreakerState.CLOSED
                self._failure_count = 0
                self._last_failure_time = None
            elif self._state == CircuitBreakerState.CLOSED:
                self._failure_count = max(0, self._failure_count - 1)

    async def record_failure(self) -> None:
        async with self._lock:
            await self._record_failure_locked()

    async def _record_failure_locked(self) -> None:
        now = time.monotonic()
        if self._state == CircuitBreakerState.OPEN:
            self._last_failure_time = now
            return
        if self._state == CircuitBreakerState.HALF_OPEN:
            logger.warning(
                "Circuit breaker %s: half-open → open (failure in probe)",
                self._name,
            )
            self._state = CircuitBreakerState.OPEN
            self._failure_count = self._failure_threshold
            self._last_failure_time = now
            return
        # CLOSED
        self._failure_count += 1
        self._last_failure_time = now
        if self._failure_count >= self._failure_threshold:
            logger.warning(
                "Circuit breaker %s: closed → open after %d failures (cooldown=%.0fs)",
                self._name,
                self._failure_count,
                self._cooldown_seconds,
            )
            self._state = CircuitBreakerState.OPEN

    async def allow_request(self) -> bool:
        """尝试进入 HALF_OPEN 试探态；返回 True 表示允许请求。"""
        if not self._enabled:
            return True
        async with self._lock:
            if self._state == CircuitBreakerState.CLOSED:
                return True
            if self._state == CircuitBreakerState.OPEN:
                if self._cooldown_has_expired():
                    logger.info(
                        "Circuit breaker %s: open → half-open (cooldown expired)",
                        self._name,
                    )
                    self._state = CircuitBreakerState.HALF_OPEN
                    return True
                return False
            # HALF_OPEN: 只允许一次试探请求
            return True

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self._failure_threshold,
            "cooldown_seconds": self._cooldown_seconds,
            "enabled": self._enabled,
        }


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
