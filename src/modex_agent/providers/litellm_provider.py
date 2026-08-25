"""LiteLLM Provider实现

使用LiteLLM库统一调用100+ LLM模型。
支持: OpenAI, Anthropic, Azure, Cohere, Mistral, MiniMax等
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any, ClassVar

from modex_agent.core.constants import DefaultValues, FinishReason, ReasoningEffort, ToolChoice
from modex_agent.core.llm_struct import (
    RuntimeSafetyPolicy,
    build_timeout_response,
    classify_litellm_error,
)
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.tool_call_accumulator import (
    ToolCallAccumulator,
    parse_tool_call_chunks_from_delta,
)
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.providers.shared.constants import inject_cache_control, inject_reasoning_effort
from modex_agent.utils.think_tag import ThinkTagExtractor

_LITELLM_AVAILABLE = importlib.util.find_spec("litellm") is not None

logger = logging.getLogger(__name__)


class LiteLLMProvider(StreamingLLMProvider):
    """
    基于LiteLLM的LLM Provider。

    LiteLLM支持100+模型,统一API调用方式。

    Example:
        provider = LiteLLMProvider(model="gpt-4", api_key="sk-...")
        response = await provider.chat([ChatMessage(role="user", content="Hello")])
        async for event in provider.chat_stream(messages):
            print(event.content, end="")
    """

    _API_MSG_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "role",
            "content",
            "name",
            "tool_calls",
            "tool_call_id",
            "function_call",
        }
    )

    @staticmethod
    def _sanitize_api_messages(
        messages: list[ChatMessage],
    ) -> list[dict[str, Any]]:
        """Filter ChatMessage structs to the LiteLLM API message fields.

        Uses ``to_dict()`` (not ``model_dump()``) so that ``tool_calls``
        serializes to the OpenAI wire format (``id``/``type``/``function``)
        and ``arguments`` becomes a JSON string.
        """
        allowed = LiteLLMProvider._API_MSG_FIELDS
        result: list[dict[str, Any]] = []
        for msg in messages:
            raw = msg.to_dict()
            result.append({k: v for k, v in raw.items() if k in allowed})
        return result

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = DefaultValues.TEMPERATURE,
        top_p: float = DefaultValues.TOP_P,
        max_output_tokens: int | None = None,
        timeout: float | None = None,
        stream_idle_timeout: float | None = None,
        parse_think_tags: bool = True,
        reasoning_effort: ReasoningEffort = ReasoningEffort.NONE,
        safety: RuntimeSafetyPolicy | None = None,
        **kwargs,
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._temperature = temperature
        self._top_p = top_p
        self._max_output_tokens = max_output_tokens
        self._extra_kwargs = kwargs

        if not _LITELLM_AVAILABLE:
            raise ImportError(
                "litellm is required for LiteLLMProvider. "
                "Install with: pip install litellm"
            )

        import litellm

        litellm.suppress_debug_info = True
        self._acompletion = litellm.acompletion
        self._parse_think_tags = parse_think_tags
        self._reasoning_effort = reasoning_effort

        # Apply safety policy overrides if provided
        if safety is not None:
            self._timeout = safety.llm.request_timeout_seconds
            self._stream_idle_timeout = safety.llm.stream_idle_timeout_seconds
        else:
            self._timeout = timeout
            self._stream_idle_timeout = stream_idle_timeout

        retry_backoff = safety.llm.retry_backoff_seconds if safety is not None else (2.0, 8.0)
        super().__init__(retry_backoff_seconds=retry_backoff)

        logger.info(
            "LiteLLMProvider created: model=%s base_url=%s "
            "request_timeout=%s stream_idle_timeout=%s safety_applied=%s",
            self._model,
            base_url,
            self._timeout,
            self._stream_idle_timeout,
            safety is not None,
        )

    def get_default_model(self) -> str:
        return self._model

    def _get_attr_or_extra(self, obj: Any, attr_name: str) -> Any:
        if hasattr(obj, attr_name):
            val = getattr(obj, attr_name)
            if val is not None:
                return val

        if hasattr(obj, "model_extra") and obj.model_extra:
            return obj.model_extra.get(attr_name)

        return None

    @staticmethod
    async def _invoke_callback(callback: Callable[[str], Any] | None, value: str) -> None:
        if callback is None or not value:
            return
        result = callback(value)
        import asyncio

        if asyncio.iscoroutine(result):
            await result

    def _extract_delta(self, chunk: Any) -> dict[str, Any]:
        result = {}

        if hasattr(chunk, "choices") and len(chunk.choices) > 0:
            choice = chunk.choices[0]
            if hasattr(choice, "delta") and choice.delta:
                delta = choice.delta

                content = self._get_attr_or_extra(delta, "content")
                if content is not None:
                    result["content"] = content

                reasoning_content = self._get_attr_or_extra(delta, "reasoning_content")
                if reasoning_content is None:
                    reasoning_content = self._get_attr_or_extra(delta, "reasoning")
                if reasoning_content is not None:
                    result["reasoning_content"] = reasoning_content

                tool_calls = self._get_attr_or_extra(delta, "tool_calls")
                if tool_calls:
                    result["tool_calls"] = tool_calls

            if hasattr(choice, "finish_reason") and choice.finish_reason:
                result["finish_reason"] = choice.finish_reason

        elif isinstance(chunk, dict) and "choices" in chunk and len(chunk["choices"]) > 0:
            choice = chunk["choices"][0]
            if "delta" in choice:
                return dict(choice["delta"])

        return result

    def _build_request_params(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        session_id = kwargs.pop("prompt_cache_key", "")
        params = {
            "model": model or self._model,
            "messages": self._sanitize_api_messages(messages),
            "api_key": self._api_key,
            "base_url": self._base_url,
            "temperature": temperature if temperature is not None else self._temperature,
            "top_p": self._top_p,
            "max_tokens": max_output_tokens if max_output_tokens is not None else self._max_output_tokens,
            **self._extra_kwargs,
            **kwargs,
        }
        if self._timeout is not None:
            params["timeout"] = self._timeout

        if stream:
            params["stream"] = True
            params["stream_options"] = {"include_usage": True}

        inject_reasoning_effort(params, self._reasoning_effort)
        inject_cache_control(params, session_id)

        if tools:
            params["tools"] = tools
            if "tool_choice" not in params:
                params["tool_choice"] = ToolChoice.AUTO.value

        return params

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
        return await self.chat_stream_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs,
        )

    async def chat_stream_with_retry(
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
        """带重试的流式调用。temperature=None 时回退到构造函数/配置值。"""
        return await self._execute_with_retry(
            self._chat_stream_raw,
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

    async def _chat_stream_raw(
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
        params = self._build_request_params(
            messages=messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            stream=True,
            **kwargs,
        )

        t0 = time.monotonic()
        logger.info(
            "LiteLLM stream start: model=%s messages=%d "
            "request_timeout=%s stream_idle_timeout=%s",
            params.get("model"),
            len(messages),
            self._timeout,
            self._stream_idle_timeout,
        )
        try:
            response = await self._acompletion(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            error_info = classify_litellm_error(exc)
            logger.warning(
                "LiteLLM stream failed: kind=%s exc_type=%s provider=%s "
                "elapsed=%.0fms message=%s",
                error_info.kind.value,
                type(exc).__name__,
                error_info.provider,
                elapsed_ms,
                error_info.message[:200],
            )
            return LLMResponse(
                content=f"Error calling LLM: {error_info.message}",
                finish_reason=FinishReason.ERROR,
                error=error_info.message,
                error_info=error_info,
            )

        accumulator = ToolCallAccumulator()
        emitted_tool_ids: set = set()
        tool_calls: list[ToolCall] = []
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict = {}
        think_extractor = ThinkTagExtractor() if self._parse_think_tags else None
        has_native_reasoning = False
        first_token_time: float | None = None

        def _add_tool_call(tool_call: ToolCall) -> None:
            tool_key = f"{tool_call.tool_name}:{json.dumps(tool_call.arguments, sort_keys=True)}"
            if tool_key not in emitted_tool_ids:
                emitted_tool_ids.add(tool_key)
                tool_calls.append(tool_call)

        iterator = response.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    anext(iterator),
                    timeout=self._stream_idle_timeout,
                )
            except StopAsyncIteration:
                break
            except TimeoutError:
                with contextlib.suppress(Exception):
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close
                partial_content = "".join(content_parts)
                logger.warning(
                    "LiteLLM stream idle timeout after %.1fs, partial_content_len=%d",
                    self._stream_idle_timeout,
                    len(partial_content),
                )
                return build_timeout_response(
                    provider="litellm",
                    message="LLM stream idle timeout",
                    partial_content=partial_content,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Mid-stream error raised while iterating (e.g. content
                # moderation). Convert to a graceful error response preserving
                # partial content already streamed, instead of aborting the turn.
                with contextlib.suppress(Exception):
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        await close
                error_info = classify_litellm_error(exc)
                partial_content = "".join(content_parts)
                logger.warning(
                    "LiteLLM stream failed mid-stream: kind=%s exc_type=%s "
                    "partial_content_len=%d message=%s",
                    error_info.kind.value,
                    type(exc).__name__,
                    len(partial_content),
                    error_info.message[:200],
                )
                return LLMResponse(
                    content=partial_content,
                    finish_reason=FinishReason.ERROR,
                    error=error_info.message,
                    error_info=error_info,
                )

            chunk_usage = self._get_attr_or_extra(chunk, "usage")
            if chunk_usage is not None:
                usage = _extract_litellm_usage(chunk_usage)

            delta = self._extract_delta(chunk)
            if not delta:
                continue

            if "tool_calls" in delta and delta["tool_calls"]:
                tool_chunks = parse_tool_call_chunks_from_delta(delta["tool_calls"])
                for tool_chunk in tool_chunks:
                    completed_tool_calls = accumulator.add_chunk(tool_chunk)
                    for tool_call in completed_tool_calls:
                        _add_tool_call(tool_call)

            # 原生 reasoning_content 优先级最高；一旦出现即持久，不再回落 think 提取
            if "reasoning_content" in delta and delta["reasoning_content"]:
                if first_token_time is None:
                    first_token_time = time.time()
                has_native_reasoning = True
                reasoning_delta = delta["reasoning_content"]
                reasoning_parts.append(reasoning_delta)
                await self._invoke_callback(on_reasoning_delta, reasoning_delta)

            # 处理普通 content；若开启 parse_think_tags 且无原生 reasoning，则做 tag 剥离
            if "content" in delta and delta["content"]:
                if first_token_time is None:
                    first_token_time = time.time()
                if think_extractor and not has_native_reasoning:
                    content_delta, reasoning_delta = think_extractor.feed(delta["content"])
                    if reasoning_delta:
                        reasoning_parts.append(reasoning_delta)
                        await self._invoke_callback(on_reasoning_delta, reasoning_delta)
                    if content_delta:
                        content_parts.append(content_delta)
                        await self._invoke_callback(on_content_delta, content_delta)
                else:
                    content_parts.append(delta["content"])
                    await self._invoke_callback(on_content_delta, delta["content"])

            if "finish_reason" in delta and delta["finish_reason"]:
                finish_reason = delta["finish_reason"].lower()

        # finish_reason=length means the stream was cut at the max_tokens
        # ceiling: pending tool calls are truncated mid-arguments, and
        # repairing them into executable calls is unsafe (W0 audit P4).
        # Drop them; every other ending keeps the partial flush.
        if finish_reason != FinishReason.LENGTH.value:
            for tool_call in accumulator.flush_pending():
                _add_tool_call(tool_call)

        # Flush any remaining buffered content from think extractor
        if think_extractor:
            flush_content, _ = think_extractor.flush()
            if flush_content:
                content_parts.append(flush_content)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "LLM stream attempt done: model=%s finish=%s content_len=%d elapsed=%.0fms",
            params.get("model"),
            finish_reason,
            len("".join(content_parts)),
            elapsed_ms,
        )

        completion_start_time: str | None = None
        if first_token_time is not None:
            from datetime import datetime
            completion_start_time = datetime.fromtimestamp(
                first_token_time, tz=UTC
            ).isoformat()

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
            finish_reason=FinishReason(finish_reason) if finish_reason else FinishReason.STOP,
            usage=usage,
            completion_start_time=completion_start_time,
            response_id=getattr(response, "id", None),
        )


def _extract_litellm_usage(usage_obj: Any) -> dict[str, int]:
    """Extract token counts from a litellm usage object into a flat dict.

    litellm returns usage as either a pydantic model (Usage) or a dict.
    Key names vary across providers (input_tokens vs prompt_tokens).
    """
    if isinstance(usage_obj, dict):
        return {k: v for k, v in usage_obj.items() if isinstance(v, int)}
    result: dict[str, int] = {}
    for attr in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "input_tokens", "output_tokens",
        "reasoning_tokens",
        "cache_read_input_tokens", "cache_creation_input_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    ):
        val = getattr(usage_obj, attr, None)
        if val is not None and isinstance(val, int | float):
            result[attr] = int(val)

    prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
    if prompt_details is not None:
        cached = getattr(prompt_details, "cached_tokens", None)
        if cached is not None and isinstance(cached, int | float):
            result["cache_read_input_tokens"] = int(cached)

    completion_details = getattr(usage_obj, "completion_tokens_details", None)
    if completion_details is not None:
        reasoning = getattr(completion_details, "reasoning_tokens", None)
        if reasoning is not None and isinstance(reasoning, int | float):
            result["reasoning_tokens"] = int(reasoning)
    return result
