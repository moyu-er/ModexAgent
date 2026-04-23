"""LiteLLM Provider实现

使用LiteLLM库统一调用100+ LLM模型。
支持: OpenAI, Anthropic, Azure, Cohere, Mistral, MiniMax等
"""

import json
import logging
import os
from typing import Any, Callable

os.environ["LITELLM_LOG"] = "ERROR"
os.environ["LITELLM_SUPPRESS_DEBUG"] = "true"

_SUPPRESSED_LOGGERS = [
    "litellm",
    "litellm.llm_provider",
    "litellm.utils",
    "httpx",
    "httpcore",
]
for _name in _SUPPRESSED_LOGGERS:
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.CRITICAL)
    _lg.propagate = False
    for _h in _lg.handlers[:]:
        _lg.removeHandler(_h)

from framework.core.constants import DefaultValues, ToolChoice
from framework.core.provider import StreamingLLMProvider
from framework.core.tool_call_accumulator import (
    ToolCallAccumulator,
    parse_tool_call_chunks_from_delta,
)
from framework.core.types import LLMResponse, ToolCall


class ThinkTagExtractor:
    """Stateful streaming think-tag extractor.

    Splits incoming text chunks into visible content and hidden reasoning.
    Supports incomplete tags spanning chunk boundaries.
    """

    _OPEN_TAG = "<think>"
    _CLOSE_TAG = "</think>"

    def __init__(self):
        self._in_think = False
        self._pending = ""

    def feed(self, text: str) -> tuple[str | None, str | None]:
        """Process a new chunk and return (content_delta, reasoning_delta)."""
        data = self._pending + text
        self._pending = ""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        i = 0
        n = len(data)

        while i < n:
            if self._in_think:
                close_idx = data.find(self._CLOSE_TAG, i)
                if close_idx != -1:
                    reasoning_parts.append(data[i:close_idx])
                    self._in_think = False
                    i = close_idx + len(self._CLOSE_TAG)
                    continue

                remaining = data[i:]
                max_prefix = min(len(remaining), len(self._CLOSE_TAG) - 1)
                prefix_len = 0
                for k in range(max_prefix, 0, -1):
                    if remaining.endswith(self._CLOSE_TAG[:k]):
                        prefix_len = k
                        break
                if prefix_len:
                    keep_len = len(remaining) - prefix_len
                    if keep_len > 0:
                        reasoning_parts.append(remaining[:keep_len])
                    self._pending = remaining[keep_len:]
                    i = n
                else:
                    reasoning_parts.append(remaining)
                    i = n
            else:
                open_idx = data.find(self._OPEN_TAG, i)
                if open_idx != -1:
                    content_parts.append(data[i:open_idx])
                    self._in_think = True
                    i = open_idx + len(self._OPEN_TAG)
                    continue

                remaining = data[i:]
                max_prefix = min(len(remaining), len(self._OPEN_TAG) - 1)
                prefix_len = 0
                for k in range(max_prefix, 0, -1):
                    if remaining.endswith(self._OPEN_TAG[:k]):
                        prefix_len = k
                        break
                if prefix_len:
                    keep_len = len(remaining) - prefix_len
                    if keep_len > 0:
                        content_parts.append(remaining[:keep_len])
                    self._pending = remaining[keep_len:]
                    i = n
                else:
                    content_parts.append(remaining)
                    i = n

        content = "".join(content_parts) if content_parts else None
        reasoning = "".join(reasoning_parts) if reasoning_parts else None
        return content, reasoning

    @classmethod
    def extract(cls, text: str) -> tuple[str, str | None]:
        """One-shot extraction for complete non-streaming text."""
        extractor = cls()
        content, reasoning = extractor.feed(text)
        return content or "", reasoning


class LiteLLMProvider(StreamingLLMProvider):
    """
    基于LiteLLM的LLM Provider。

    LiteLLM支持100+模型,统一API调用方式。

    Example:
        provider = LiteLLMProvider(model="gpt-4", api_key="sk-...")
        response = await provider.chat([{"role": "user", "content": "Hello"}])
        async for event in provider.chat_stream(messages):
            print(event.content, end="")
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = DefaultValues.TEMPERATURE,
        max_tokens: int | None = None,
        timeout: float = DefaultValues.TIMEOUT_SECONDS,
        parse_think_tags: bool = False,
        reasoning_effort: str | None = None,
        **kwargs,
    ):
        try:
            import litellm
            from litellm import acompletion

            litellm.suppress_debug_info = True
            litellm.set_verbose = False

        except ImportError as err:
            raise ImportError(
                "litellm is required for LiteLLMProvider. Install with: pip install litellm"
            ) from err

        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._extra_kwargs = kwargs
        self._acompletion = acompletion
        self._think_extractor = ThinkTagExtractor() if parse_think_tags else None
        self._reasoning_effort = reasoning_effort

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
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        params = {
            "model": model or self._model,
            "messages": messages,
            "api_key": self._api_key,
            "base_url": self._base_url,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "timeout": self._timeout,
            **self._extra_kwargs,
            **kwargs,
        }

        if stream:
            params["stream"] = True

        if self._reasoning_effort is not None:
            params["reasoning_effort"] = self._reasoning_effort

        if tools:
            params["tools"] = tools
            params["tool_choice"] = ToolChoice.AUTO.value

        return params

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self.chat_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            **kwargs,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 3,
        **kwargs,
    ) -> LLMResponse:
        return await self._execute_with_retry(
            self._chat_raw, messages, max_retries,
            model=model, temperature=temperature, max_tokens=max_tokens, tools=tools, **kwargs
        )

    async def _chat_raw(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params = self._build_request_params(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            stream=False,
            **kwargs,
        )
        response = await self._acompletion(**params)

        if not hasattr(response, "choices") or not response.choices:
            return LLMResponse(content=None, error="Empty response from LLM")

        choice = response.choices[0]
        msg = getattr(choice, "message", None)
        if not msg:
            return LLMResponse(content=None, error="Empty message in response")

        raw_content = self._get_attr_or_extra(msg, "content") or ""
        reasoning = self._get_attr_or_extra(msg, "reasoning_content")
        if reasoning is None:
            reasoning = self._get_attr_or_extra(msg, "reasoning")

        # parse_think_tags fallback for non-streaming response
        if self._think_extractor and reasoning is None:
            clean_content, extracted_reasoning = ThinkTagExtractor.extract(raw_content)
            raw_content = clean_content
            reasoning = extracted_reasoning
        else:
            clean_content = raw_content

        tool_calls = []
        raw_tool_calls = self._get_attr_or_extra(msg, "tool_calls")
        if raw_tool_calls:
            for tc in raw_tool_calls:
                tool_calls.append(ToolCall(
                    tool_name=tc.get("function", {}).get("name", ""),
                    arguments=json.loads(tc.get("function", {}).get("arguments", "{}")),
                    call_id=tc.get("id"),
                ))

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = dict(response.usage) if hasattr(response.usage, "__iter__") else {}

        finish_reason = choice.finish_reason if hasattr(choice, "finish_reason") else "stop"

        return LLMResponse(
            content=clean_content,
            tool_calls=tool_calls,
            reasoning_content=reasoning,
            finish_reason=finish_reason or "stop",
            usage=usage,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self.chat_stream_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 3,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self._execute_with_retry(
            self._chat_stream_raw, messages, max_retries,
            model=model, temperature=temperature, max_tokens=max_tokens, tools=tools,
            on_content_delta=on_content_delta, on_reasoning_delta=on_reasoning_delta, **kwargs
        )

    async def _chat_stream_raw(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params = self._build_request_params(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            stream=True,
            **kwargs,
        )

        response = await self._acompletion(**params)

        accumulator = ToolCallAccumulator()
        emitted_tool_ids: set = set()
        tool_calls: list[ToolCall] = []
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict = {}

        def _add_tool_call(tool_call: ToolCall) -> None:
            tool_key = f"{tool_call.tool_name}:{json.dumps(tool_call.arguments, sort_keys=True)}"
            if tool_key not in emitted_tool_ids:
                emitted_tool_ids.add(tool_key)
                tool_calls.append(tool_call)

        async for chunk in response:
            delta = self._extract_delta(chunk)
            if not delta:
                continue

            if "tool_calls" in delta and delta["tool_calls"]:
                tool_chunks = parse_tool_call_chunks_from_delta(delta["tool_calls"])
                for tool_chunk in tool_chunks:
                    completed_tool_calls = accumulator.add_chunk(tool_chunk)
                    for tool_call in completed_tool_calls:
                        _add_tool_call(tool_call)

            # 原生 reasoning_content 优先级最高
            has_native_reasoning = "reasoning_content" in delta and delta["reasoning_content"]
            if has_native_reasoning:
                reasoning_delta = delta["reasoning_content"]
                reasoning_parts.append(reasoning_delta)
                await self._invoke_callback(on_reasoning_delta, reasoning_delta)

            # 处理普通 content；若开启 parse_think_tags 且无原生 reasoning，则做 tag 剥离
            if "content" in delta and delta["content"]:
                if self._think_extractor and not has_native_reasoning:
                    content_delta, reasoning_delta = self._think_extractor.feed(delta["content"])
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
                finish_reason = delta["finish_reason"]

        pending_tools = accumulator.flush_pending()
        for tool_call in pending_tools:
            _add_tool_call(tool_call)

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
            finish_reason=finish_reason or "stop",
            usage=usage,
        )
