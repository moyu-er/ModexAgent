"""OpenAI Provider -- native openai SDK integration.

Uses openai.AsyncOpenAI for Chat Completions API.
All response parsing goes through shared intermediate types
(StreamDelta, ParsedResponse) -- no hasattr/getattr, no bare dicts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI
import httpx

from framework.core.constants import FinishReason
from framework.core.llm_struct import (
    RuntimeSafetyPolicy,
    build_timeout_response,
)
from framework.core.provider import StreamingLLMProvider
from framework.core.tool_call_accumulator import ToolCallAccumulator
from framework.core.types import LLMResponse
from framework.providers.shared.delta import ParsedResponse, StreamDelta
from framework.providers.shared.errors import classify_openai_error
from framework.utils.think_tag import ThinkTagExtractor

logger = logging.getLogger(__name__)


class OpenAIProvider(StreamingLLMProvider):
    """LLM provider using the openai official SDK.

    Supports:
    - Full Chat Completions API (streaming + non-streaming)
    - Custom base_url (proxies/gateways)
    - Custom headers (extra_headers)
    - reasoning_content extraction (via Pydantic model_extra)

    Example:
        provider = OpenAIProvider(model="gpt-4o", api_key="sk-...")
        response = await provider.chat([{"role": "user", "content": "Hello"}])
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 45.0,
        stream_idle_timeout: float = 90.0,
        parse_think_tags: bool = True,
        reasoning_effort: str | None = None,
        extra_headers: dict[str, str] | None = None,
        safety: RuntimeSafetyPolicy | None = None,
    ):
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._extra_headers = extra_headers
        self._parse_think_tags = parse_think_tags

        if safety is not None:
            self._timeout = safety.llm.request_timeout_seconds
            self._stream_idle_timeout = safety.llm.stream_idle_timeout_seconds
        else:
            self._timeout = timeout
            self._stream_idle_timeout = stream_idle_timeout

        retry_backoff = (
            safety.llm.retry_backoff_seconds
            if safety is not None
            else (2.0, 8.0)
        )
        super().__init__(retry_backoff_seconds=retry_backoff)

        self._client = AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url,
            default_headers=extra_headers,
            timeout=httpx.Timeout(self._timeout),
            max_retries=0,
        )

    def get_default_model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
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
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 1,
        **kwargs,
    ) -> LLMResponse:
        return await self._execute_with_retry(
            self._chat_raw, messages, max_retries,
            model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, **kwargs
        )

    async def _chat_raw(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params = self._build_params(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, tools=tools, stream=False, **kwargs,
        )
        t0 = time.monotonic()
        logger.debug("OpenAI chat start: model=%s", params["model"])

        try:
            response = await self._client.chat.completions.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            error_info = classify_openai_error(exc)
            logger.warning(
                "OpenAI chat failed: kind=%s elapsed=%.0fms message=%s",
                error_info.kind.value, elapsed_ms, error_info.message[:200],
            )
            return LLMResponse(
                content=f"Error calling LLM: {error_info.message}",
                finish_reason=FinishReason.ERROR.value,
                error=error_info.message,
                error_info=error_info,
            )

        try:
            parsed = ParsedResponse.from_openai(response)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.warning(
                "OpenAI response parse failed: model=%s elapsed=%.0fms error=%s",
                params["model"], elapsed_ms, exc,
            )
            return LLMResponse(
                content=f"Error parsing LLM response: {exc}",
                finish_reason=FinishReason.ERROR.value,
                error=str(exc),
            )

        # ThinkTag fallback for non-streaming response
        content = parsed.content or ""
        reasoning = parsed.reasoning_content
        if self._parse_think_tags and reasoning is None:
            clean_content, extracted_reasoning = ThinkTagExtractor.extract(content)
            content = clean_content
            reasoning = extracted_reasoning

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "OpenAI chat done: model=%s finish=%s elapsed=%.0fms",
            params["model"], parsed.finish_reason, elapsed_ms,
        )

        return LLMResponse(
            content=content,
            tool_calls=parsed.tool_calls,
            reasoning_content=reasoning,
            finish_reason=parsed.finish_reason,
            usage=parsed.usage,
        )

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
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 0,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self._execute_with_retry(
            self._chat_stream_raw, messages, max_retries,
            model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs
        )

    async def _chat_stream_raw(
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
        params = self._build_params(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, tools=tools, stream=True, **kwargs,
        )
        t0 = time.monotonic()
        logger.debug("OpenAI stream start: model=%s", params["model"])

        try:
            stream = await self._client.chat.completions.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            error_info = classify_openai_error(exc)
            logger.warning(
                "OpenAI stream failed: kind=%s elapsed=%.0fms message=%s",
                error_info.kind.value, elapsed_ms, error_info.message[:200],
            )
            return LLMResponse(
                content=f"Error calling LLM: {error_info.message}",
                finish_reason=FinishReason.ERROR.value,
                error=error_info.message,
                error_info=error_info,
            )

        accumulator = ToolCallAccumulator()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        has_native_reasoning = False
        think_extractor = ThinkTagExtractor() if self._parse_think_tags else None

        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._stream_idle_timeout,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await stream.close()
                partial_content = "".join(content_parts)
                logger.warning(
                    "OpenAI stream idle timeout after %.1fs, partial_content_len=%d",
                    self._stream_idle_timeout, len(partial_content),
                )
                return build_timeout_response(
                    provider="openai",
                    message="LLM stream idle timeout",
                    partial_content=partial_content,
                )

            if not chunk.choices:
                continue

            delta = StreamDelta.from_openai(chunk.choices[0].delta)

            chunk_finish = chunk.choices[0].finish_reason
            if chunk_finish is not None:
                finish_reason = chunk_finish

            if delta.reasoning_content:
                has_native_reasoning = True
                reasoning_parts.append(delta.reasoning_content)
                await self._invoke_callback(on_reasoning_delta, delta.reasoning_content)

            if delta.content:
                if think_extractor and not has_native_reasoning:
                    clean_delta, extracted_reasoning = think_extractor.feed(delta.content)
                    if extracted_reasoning:
                        reasoning_parts.append(extracted_reasoning)
                        await self._invoke_callback(on_reasoning_delta, extracted_reasoning)
                    if clean_delta:
                        content_parts.append(clean_delta)
                        await self._invoke_callback(on_content_delta, clean_delta)
                else:
                    content_parts.append(delta.content)
                    await self._invoke_callback(on_content_delta, delta.content)

            if delta.tool_call_chunks:
                for tc_chunk in delta.tool_call_chunks:
                    accumulator.add_chunk(tc_chunk)

            if chunk.usage is not None:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }

        pending_tools = accumulator.flush_pending()
        all_tool_calls = accumulator.get_completed() + pending_tools

        # Flush any remaining buffered content from think extractor
        if think_extractor:
            flush_content, _ = think_extractor.flush()
            if flush_content:
                content_parts.append(flush_content)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "OpenAI stream done: model=%s finish=%s content_len=%d elapsed=%.0fms",
            params["model"], finish_reason, len("".join(content_parts)), elapsed_ms,
        )

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=all_tool_calls,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
            finish_reason=finish_reason or "stop",
            usage=usage,
        )

    @staticmethod
    async def _invoke_callback(callback: Callable[[str], Any] | None, value: str) -> None:
        if callback is None or not value:
            return
        result = callback(value)
        if asyncio.iscoroutine(result):
            await result

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "stream": stream,
        }

        if self._reasoning_effort is not None:
            params["reasoning_effort"] = self._reasoning_effort

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        # stream_options not set by default — third-party endpoints
        # (MiniMax, DeepSeek, etc.) may reject it. Pass via **kwargs if needed.

        if self._extra_headers:
            params["extra_headers"] = self._extra_headers

        params.update(kwargs)
        return params
