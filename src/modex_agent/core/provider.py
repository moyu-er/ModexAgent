"""LLM Provider抽象基类（事件流原语，ADR-0046）。"""

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from typing import Any

from .constants import FinishReason
from .llm_request import LLMRequest
from .llm_struct import LLMErrorInfo, LLMErrorKind
from .message import ChatMessage
from .stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from .types import LLMResponse, TokenUsage

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """
    LLM提供商抽象基类。

    ``stream(request)`` 是唯一流式原语：子类把协议流翻译为
    ``LLMStreamEvent`` 序列（每个流以恰好一个 Finish/StreamFailure 终结）。
    ``chat_stream`` 在此为具体实现——把事件流经 EventAssembler 折叠回带
    增量回调的 ``LLMResponse``；``chat()`` 经内部 chat_stream 重试
    （max_retries=1）收敛到同一事件流，以获得 prompt cache 收益。

    Example:
        class EchoProvider(LLMProvider):
            def get_default_model(self) -> str:
                return "echo-model"

            async def stream(
                self, request: LLMRequest
            ) -> AsyncIterator[LLMStreamEvent]:
                yield TextDelta(text="hello")
                yield Finish(finish_reason=FinishReason.STOP)

    --- Dormant mechanism-A seam (NOT implemented in the current change). ---
    When a ``Modality`` beyond ``TEXT`` is enabled on
    ``LLMConfig.capabilities`` (``ModelCapabilities``, ADR-0013 §9), the
    provider-side renderer turns each gate-accepted Attachment whose
    ``kind`` matches an enabled modality into a native multimodal content
    block (image → ``image_url``), inlined into the user message's
    ``content`` array alongside the text block. On model rejection, the
    renderer strips the inline block back to a text placeholder
    ``[image: <path>]`` / ``[doc: <path>]`` — a seamless degradation to the
    mechanism-B path-reference form (strip/restore memory discipline). When
    activated, it will be built on the event-stream primitive above, not
    revived from cold storage.

    Memory discipline (load-bearing, mechanism A): a multimodal block is
    transient at call time only — it is stripped to a placeholder before the
    message is persisted to the agent LLM history, never fed to memory
    consolidation/compression, and only the current turn's attachment is
    re-rendered; historical attachments stay as text placeholders.

    This is **design only**. The framework's provider layer does not yet
    pass multimodal content blocks through to any underlying LLM API, and
    no ``Modality`` beyond ``TEXT`` is populated on any provider config in
    v1. Every attachment reaches the agent as a path reference (mechanism
    B) until a separate spec activates this seam. See ADR-0013 §9, §10,
    §10a.
    """

    def __init__(self, retry_backoff_seconds: tuple[float, ...] = (2.0, 8.0)) -> None:
        self._retry_backoff_seconds = retry_backoff_seconds

    @abstractmethod
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """
        事件流式聊天完成（唯一流式原语）。

        Args:
            request: 规范请求信封（采样参数唯一载体）

        Yields:
            LLMStreamEvent——每个流必须以恰好一个 Finish 或 StreamFailure
            终结（EventAssembler 终态不变量）。
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """获取默认模型名称"""
        pass

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """流式聊天完成（事件流的回调折叠）。

        参数合并进 ``LLMRequest``：model 为 None 时取 provider 默认模型（信封 model
        必填）；temperature/max_output_tokens 透传（None 保持 None，兜底在
        引擎/config 的 build_body）。kwargs 中只认 ``prompt_cache_key``
        （提取进 request），其余未知 kwargs 记 ERROR 日志后丢弃、永不进入
        请求 body。

        Args:
            messages: ChatMessage 列表（结构化消息，B6 收敛自 list[dict]）
            model: 模型名称，None则使用默认模型
            temperature: 温度参数，None 时回退到构造函数/配置中的值
            max_output_tokens: 最大token数
            tools: 工具定义列表（可选）
            on_content_delta: 内容片段回调（支持 async）
            on_reasoning_delta: 推理片段回调（支持 async）
            **kwargs: 其他提供商特定参数

        Returns:
            LLM统一响应结构 LLMResponse（由事件序列组装）
        """
        # 延迟导入：providers.http.assembler 依赖 core（模块级会循环）；
        # core→providers 边界登记见 tests/architecture/test_dependency_tree.py。
        from modex_agent.providers.http.assembler import EventAssembler

        prompt_cache_key = kwargs.pop("prompt_cache_key", None)
        for key in kwargs:
            logger.error("dropping unknown chat_stream kwarg: %s", key)
        request = LLMRequest(
            model=model or self.get_default_model(),
            messages=messages,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tuple(tools) if tools else (),
            prompt_cache_key=prompt_cache_key,
        )
        assembler = EventAssembler(on_content_delta, on_reasoning_delta)
        async for event in self.stream(request):
            await assembler.feed(event)
        return assembler.result()

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """非流式聊天完成。

        对外行为与旧 LLMProvider.chat() 一致：调用者拿到完整 LLMResponse，
        无任何 delta 回调。内部经 chat_stream 折叠实现并带一次重试
        （max_retries=1），以获得 prompt cache 等只有 streaming 模式才有的
        收益。temperature=None 时回退到构造函数/配置值。
        """
        return await self._chat_stream_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            on_content_delta=None,
            on_reasoning_delta=None,
            max_retries=1,
            **kwargs,
        )

    async def _chat_stream_with_retry(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 0,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """调用 chat_stream() 并在遇到临时错误时重试。

        流式路径默认不参与自动重试（max_retries=0）。partial content 可能
        已通过 on_content_delta 回调发送给用户，重试会造成重复 delta。
        temperature=None 时回退到构造函数/配置值。
        """
        return await self._execute_with_retry(
            self.chat_stream,
            messages,
            max_retries,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs,
        )

    async def _execute_with_retry(self, fn, messages, max_retries, **kwargs):
        """带指数退避的重试执行器，同时处理异常和 error 响应。

        当 fn() 返回 LLMResponse(finish_reason=ERROR) 时按 error_info.should_retry
        决策是否重试；当 fn() 抛出异常时沿用原有 _is_transient 逻辑。
        """
        backoff_delays = self._retry_backoff_seconds
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
            if response.finish_reason != FinishReason.ERROR:
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
            finish_reason=FinishReason.ERROR,
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


class CallbackStreamProvider(LLMProvider):
    """
    回调式 provider 的显式适配基类（cassette 录制/回放、委托代理、
    脚本化测试 provider）。

    新 provider 直接在 ``LLMProvider`` 上实现 ``stream()``；本基类为
    响应级（``chat_stream``）实现而存在——其具体 ``stream()`` 把
    chat_stream 的回调流桥接为事件流（回调-事件桥接，ADR-0046）。
    """

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """
        流式聊天完成（回调原语）。

        Args:
            messages: ChatMessage 列表（结构化消息，B6 收敛自 list[dict]）
            model: 模型名称
            temperature: 温度参数，None 时回退到构造函数/配置中的值
            max_output_tokens: 最大token数
            tools: 工具定义列表（可选）
            on_content_delta: 内容片段回调（支持 async）
            on_reasoning_delta: 推理片段回调（支持 async）
            **kwargs: 其他提供商特定参数

        Returns:
            LLM统一响应结构 LLMResponse（包含完整内容和元数据）
        """
        pass

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """把 chat_stream 的回调流桥接为事件流（回调-事件桥接，ADR-0046）。

        kwargs 面逐项复刻 ReactLlmClient 现行调用（llm_client.py:139-147），
        刻意不传 ``model=``——cassette 的 llm_call_key 把 model 作为键输入，
        现客户端从不传，传了会让全部存量 cassette 键失配。

        chat_stream 返回后补译回调流带不到的完整载荷：未流出过的
        content/reasoning 补发 TextDelta/ReasoningDelta；tool_calls 逐个补发
        ToolCallComplete（tool_calls 只存在于返回值——不补译则 ReAct 循环对
        一切桥接路径断掉）；非全零默认 usage 补发 UsageSnapshot；最终以
        Finish（ERROR 时为 StreamFailure）终结。chat_stream 抛异常时以
        StreamFailure 终结、不向上抛；CancelledError 翻译为
        Finish(CANCELLED)。生成器被 aclose 或消费者中断时取消后台任务，
        不泄漏。

        意义：仅覆写 chat_stream 的回调式 provider（40+ 既有 mock 与委托
        代理）经由本方法自动获得事件流视图，零迁移。
        """
        queue: asyncio.Queue[LLMStreamEvent | None] = asyncio.Queue()
        streamed_text = False
        streamed_reasoning = False

        async def _on_content_delta(text: str) -> None:
            nonlocal streamed_text
            if text:
                streamed_text = True
                await queue.put(TextDelta(text=text))

        async def _on_reasoning_delta(text: str) -> None:
            nonlocal streamed_reasoning
            if text:
                streamed_reasoning = True
                await queue.put(ReasoningDelta(text=text))

        async def _run() -> None:
            try:
                response = await self.chat_stream(
                    messages=request.messages,
                    temperature=request.temperature,
                    max_output_tokens=request.max_output_tokens,
                    tools=list(request.tools) or None,
                    on_content_delta=_on_content_delta,
                    on_reasoning_delta=_on_reasoning_delta,
                    prompt_cache_key=request.prompt_cache_key or "",
                )
                if not streamed_text and response.content:
                    await queue.put(TextDelta(text=response.content))
                if not streamed_reasoning and response.reasoning_content:
                    await queue.put(ReasoningDelta(text=response.reasoning_content))
                for tool_call in response.tool_calls:
                    await queue.put(
                        ToolCallComplete(
                            call_id=tool_call.call_id or "",
                            tool_name=tool_call.tool_name,
                            arguments=tool_call.arguments,
                        )
                    )
                if response.usage != TokenUsage():
                    await queue.put(UsageSnapshot(usage=response.usage))
                if response.finish_reason == FinishReason.ERROR:
                    # partial_content 恒空：content 已（经回调增量或上方补译）
                    # 以 TextDelta 事件送达；assembler 把 partial 拼接到已累积
                    # 正文之前，这里再带一份 response.content 会翻倍。
                    await queue.put(
                        StreamFailure(
                            error_info=response.error_info
                            or LLMErrorInfo(
                                kind=LLMErrorKind.UNKNOWN,
                                message=(response.error or "LLM stream failed")[:500],
                            ),
                            partial_content="",
                        )
                    )
                else:
                    await queue.put(Finish(finish_reason=response.finish_reason))
            except asyncio.CancelledError:
                await queue.put(Finish(finish_reason=FinishReason.CANCELLED))
            except Exception as exc:
                await queue.put(
                    StreamFailure(
                        error_info=LLMErrorInfo(
                            kind=LLMErrorKind.UNKNOWN,
                            message=str(exc)[:500],
                            should_retry=self._is_transient(exc),
                        ),
                        partial_content="",
                    )
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(_run())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield event
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
