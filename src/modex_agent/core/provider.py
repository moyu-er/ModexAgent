"""LLM Provider抽象基类"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .constants import FinishReason
from .types import LLMResponse

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """
    LLM提供商抽象基类。

    用于抽象不同LLM提供商的调用方式，支持同步调用。

    Example:
        class OpenAIProvider(LLMProvider):
            def __init__(self, api_key: str):
                super().__init__()
                self.client = OpenAI(api_key=api_key)

            async def chat(self, messages: List[Dict], **kwargs) -> LLMResponse:
                response = await self.client.chat.completions.create(
                    model=self.get_default_model(),
                    messages=messages
                )
                msg = response.choices[0].message
                return LLMResponse(content=msg.content)

            def get_default_model(self) -> str:
                return "gpt-4"
    """

    def __init__(self, retry_backoff_seconds: tuple[float, ...] = (2.0, 8.0)) -> None:
        self._retry_backoff_seconds = retry_backoff_seconds

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """
        非流式聊天完成。

        Args:
            messages: 消息列表，格式为 [{"role": "user", "content": "..."}, ...]
            model: 模型名称，None则使用默认模型
            temperature: 温度参数
            max_tokens: 最大token数
            tools: 工具定义列表（可选）
            **kwargs: 其他提供商特定参数

        Returns:
            LLM统一响应结构 LLMResponse
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """获取默认模型名称"""
        pass

    async def complete(self, prompt: str, **kwargs) -> LLMResponse:
        """
        完成单个提示词(非对话模式)。

        默认实现将prompt包装为user消息调用chat。
        子类可以重写以优化性能。

        Args:
            prompt: 提示词文本
            **kwargs: 其他参数

        Returns:
            LLM统一响应结构 LLMResponse
        """
        messages = [{"role": "user", "content": prompt}]
        return await self.chat(messages, **kwargs)

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 3,
        **kwargs,
    ) -> LLMResponse:
        """调用 chat() 并在遇到临时错误时重试"""
        return await self._execute_with_retry(
            self.chat,
            messages,
            max_retries,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            **kwargs,
        )

    async def _execute_with_retry(self, fn, messages, max_retries, **kwargs):
        """带指数退避的重试执行器，同时处理异常和 error 响应。

        当 fn() 返回 LLMResponse(finish_reason=ERROR) 时按 error_info.should_retry
        决策是否重试；当 fn() 抛出异常时沿用原有 _is_transient 逻辑。
        """
        backoff_delays = getattr(self, "_retry_backoff_seconds", (2.0, 8.0))
        last_response: LLMResponse | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await fn(messages, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt >= max_retries or not self._is_transient(e):
                    raise
                delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
                logger.warning(
                    "LLM retry attempt %d/%d after %.1fs: %s",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    str(e)[:200],
                )
                await asyncio.sleep(delay)
                continue

            # 成功获取 response，检查是否为 error response
            if response.finish_reason != FinishReason.ERROR.value:
                return response

            last_response = response
            if attempt >= max_retries:
                return response

            should_retry = (
                response.error_info.should_retry
                if response.error_info is not None
                else False  # 无 error_info 时不重试，避免对不可恢复错误反复重试
            )
            if not should_retry:
                logger.warning(
                    "LLM error response not retryable: finish_reason=%s error=%s",
                    response.finish_reason,
                    (response.error or "")[:200],
                )
                return response

            delay = backoff_delays[min(attempt, len(backoff_delays) - 1)]
            logger.warning(
                "LLM error response retry attempt %d/%d after %.1fs: %s",
                attempt + 1,
                max_retries + 1,
                delay,
                (response.error or "")[:200],
            )
            await asyncio.sleep(delay)

        return last_response or LLMResponse(
            content=None,
            finish_reason=FinishReason.ERROR.value,
            error="LLM retry failed without response",
        )

    @classmethod
    def _is_transient(cls, error: Exception) -> bool:
        """判断错误是否为临时性错误（可重试）

        覆盖的HTTP状态码和文本标记：429, 500, 502, 503, 504,
        rate limit, timeout, timed out, connection, server error,
        internal server, overloaded, temporarily unavailable,
        empty response, invalid response.
        """
        error_text = str(error).lower()
        transient_markers = (
            "429",
            "500",
            "502",
            "503",
            "504",
            "rate limit",
            "timeout",
            "timed out",
            "connection",
            "server error",
            "internal server",
            "overloaded",
            "temporarily unavailable",
            "empty response",
            "invalid response",
        )
        for marker in transient_markers:
            if marker in error_text:
                # 配额/计费错误不应重试
                return not (
                    "insufficient_quota" in error_text or "billing hard limit" in error_text
                )
        return False


class StreamingLLMProvider(LLMProvider):
    """
    支持流式输出的LLM提供商抽象基类。

    继承自LLMProvider，额外支持流式输出。
    chat() 和 chat_with_retry() 内部使用流式 API 以获得 prompt cache 收益。
    """

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """非流式聊天完成。

        对外行为与 LLMProvider.chat() 完全一致：调用者拿到完整 LLMResponse，
        无任何 delta 回调。
        内部使用流式 API 实现（chat_stream_with_retry），以获得 prompt cache
        等只有 streaming 模式才有的收益。
        """
        return await self.chat_stream_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            on_content_delta=None,
            on_reasoning_delta=None,
            max_retries=1,
            **kwargs,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 3,
        **kwargs,
    ) -> LLMResponse:
        """带重试的非流式聊天完成。

        对外行为与 LLMProvider.chat_with_retry() 完全一致：调用者拿到完整
        LLMResponse，无任何 delta 回调，失败时按 max_retries 自动重试。
        内部使用流式 API 实现（chat_stream_with_retry），以获得 prompt cache
        等只有 streaming 模式才有的收益。
        """
        return await self.chat_stream_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            on_content_delta=None,
            on_reasoning_delta=None,
            max_retries=max_retries,
            **kwargs,
        )

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """
        流式聊天完成。

        Args:
            messages: 消息列表
            model: 模型名称
            temperature: 温度参数
            max_tokens: 最大token数
            tools: 工具定义列表（可选）
            on_content_delta: 内容片段回调（支持 async）（支持 async）
            on_reasoning_delta: 推理片段回调（支持 async）（支持 async）
            **kwargs: 其他提供商特定参数

        Returns:
            LLM统一响应结构 LLMResponse（包含完整内容和元数据）
        """
        pass

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 0,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """调用 chat_stream() 并在遇到临时错误时重试。

        流式路径默认不参与自动重试（max_retries=0）。partial content 可能
        已通过 on_content_delta 回调发送给用户，重试会造成重复 delta。
        """
        return await self._execute_with_retry(
            self.chat_stream,
            messages,
            max_retries,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs,
        )
