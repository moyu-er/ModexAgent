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
from datetime import UTC
from typing import Any, ClassVar

import httpx
from openai import AsyncOpenAI

from modex_agent.core.constants import FinishReason, ReasoningEffort
from modex_agent.core.llm_struct import (
    RuntimeSafetyPolicy,
    build_timeout_response,
)
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import StreamingLLMProvider
from modex_agent.core.tool_call_accumulator import ToolCallAccumulator
from modex_agent.core.types import LLMResponse
from modex_agent.providers.shared.constants import inject_cache_control, inject_reasoning_effort
from modex_agent.providers.shared.delta import StreamDelta
from modex_agent.providers.shared.errors import classify_openai_error
from modex_agent.utils.think_tag import ThinkTagExtractor

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
        response = await provider.chat([ChatMessage(role="user", content="Hello")])
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_output_tokens: int | None = None,
        timeout: float | None = None,
        stream_idle_timeout: float | None = None,
        parse_think_tags: bool = True,
        reasoning_effort: ReasoningEffort = ReasoningEffort.NONE,
        extra_headers: dict[str, str] | None = None,
        safety: RuntimeSafetyPolicy | None = None,
    ) -> None:
        self._model = model
        self._temperature = temperature
        self._top_p = top_p
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._extra_headers = extra_headers
        self._parse_think_tags = parse_think_tags

        if safety is not None:
            self._timeout = safety.llm.request_timeout_seconds
            self._stream_idle_timeout = safety.llm.stream_idle_timeout_seconds
        else:
            # Default: no provider-level timeout (None = infinite wait).
            # The outer turn timeout (agent_run_timeout) + watchdog are the
            # sole termination mechanism. This prevents premature stream
            # interruption when the LLM takes a long time to produce a
            # large response (e.g. long document generation).
            self._timeout = timeout
            self._stream_idle_timeout = stream_idle_timeout

        retry_backoff = safety.llm.retry_backoff_seconds if safety is not None else (2.0, 8.0)
        super().__init__(retry_backoff_seconds=retry_backoff)

        self._client = AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url,
            default_headers=extra_headers,
            timeout=httpx.Timeout(self._timeout),
            max_retries=0,
        )

        logger.info(
            "OpenAIProvider created: model=%s base_url=%s "
            "request_timeout=%s stream_idle_timeout=%s safety_applied=%s",
            self._model,
            base_url,
            self._timeout,
            self._stream_idle_timeout,
            safety is not None,
        )

    def get_default_model(self) -> str:
        return self._model

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
        """Stream a completion. temperature=None falls back to the constructor/config value."""
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
        """Stream with retry. temperature=None falls back to the constructor/config value."""
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
        params = self._build_params(
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
            "OpenAI stream start: model=%s messages=%d "
            "request_timeout=%s stream_idle_timeout=%s",
            params["model"],
            len(messages),
            self._timeout,
            self._stream_idle_timeout,
        )

        try:
            stream = await self._client.chat.completions.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            error_info = classify_openai_error(exc)
            logger.warning(
                "OpenAI stream failed: kind=%s exc_type=%s elapsed=%.0fms "
                "message=%s",
                error_info.kind.value,
                type(exc).__name__,
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
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        has_native_reasoning = False
        think_extractor = ThinkTagExtractor() if self._parse_think_tags else None
        first_token_time: float | None = None

        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._stream_idle_timeout,
                )
            except StopAsyncIteration:
                break
            except TimeoutError:
                with contextlib.suppress(Exception):
                    await stream.close()
                partial_content = "".join(content_parts)
                logger.warning(
                    "OpenAI stream idle timeout after %.1fs, partial_content_len=%d",
                    self._stream_idle_timeout,
                    len(partial_content),
                )
                return build_timeout_response(
                    provider="openai",
                    message="LLM stream idle timeout",
                    partial_content=partial_content,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Mid-stream error raised by the SDK while iterating (e.g. GLM
                # content-moderation ``new_sensitive (1027)``). Convert to a
                # graceful error response preserving partial content already
                # streamed, instead of letting it abort the whole agent turn.
                with contextlib.suppress(Exception):
                    await stream.close()
                error_info = classify_openai_error(exc)
                partial_content = "".join(content_parts)
                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.warning(
                    "OpenAI stream failed mid-stream: kind=%s exc_type=%s "
                    "elapsed=%.0fms partial_content_len=%d message=%s",
                    error_info.kind.value,
                    type(exc).__name__,
                    elapsed_ms,
                    len(partial_content),
                    error_info.message[:200],
                )
                return LLMResponse(
                    content=partial_content,
                    finish_reason=FinishReason.ERROR,
                    error=error_info.message,
                    error_info=error_info,
                )

            if chunk.usage is not None:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }
                prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
                if prompt_details is not None:
                    cached = getattr(prompt_details, "cached_tokens", None)
                    if cached is not None:
                        usage["cache_read_input_tokens"] = cached
                completion_details = getattr(chunk.usage, "completion_tokens_details", None)
                if completion_details is not None:
                    reasoning = getattr(completion_details, "reasoning_tokens", None)
                    if reasoning is not None:
                        usage["reasoning_tokens"] = reasoning

            if not chunk.choices:
                continue

            delta = StreamDelta.from_openai(chunk.choices[0].delta)

            chunk_finish = chunk.choices[0].finish_reason
            if chunk_finish is not None:
                finish_reason = chunk_finish

            if delta.reasoning_content:
                if first_token_time is None:
                    first_token_time = time.time()
                has_native_reasoning = True
                reasoning_parts.append(delta.reasoning_content)
                await self._invoke_callback(on_reasoning_delta, delta.reasoning_content)

            if delta.content:
                if first_token_time is None:
                    first_token_time = time.time()
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

        # finish_reason=length means the stream was cut at the max_tokens
        # ceiling: pending tool calls are truncated mid-arguments, and
        # repairing them into executable calls is unsafe (W0 audit P4).
        # Drop them; every other ending keeps the partial flush.
        pending_tools = (
            []
            if finish_reason == FinishReason.LENGTH.value
            else accumulator.flush_pending()
        )
        all_tool_calls = accumulator.get_completed() + pending_tools

        # Flush any remaining buffered content from think extractor
        if think_extractor:
            flush_content, _ = think_extractor.flush()
            if flush_content:
                content_parts.append(flush_content)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "OpenAI stream done: model=%s finish=%s content_len=%d elapsed=%.0fms",
            params["model"],
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
            tool_calls=all_tool_calls,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
            finish_reason=FinishReason(finish_reason) if finish_reason else FinishReason.STOP,
            usage=usage,
            completion_start_time=completion_start_time,
        )

    @staticmethod
    async def _invoke_callback(callback: Callable[[str], Any] | None, value: str) -> None:
        if callback is None or not value:
            return
        result = callback(value)
        if asyncio.iscoroutine(result):
            await result

    # Standard OpenAI Chat API message fields.
    # Everything else (content_format, truncatable_paths, metadata,
    # meta_context_lossy, etc.) is governance-internal and must not
    # reach external providers.
    # reasoning_content is intentionally NOT listed: it is conditionally
    # re-attached in _sanitize_api_messages per the DeepSeek thinking-mode
    # passback rule (assistant tool-call turns only).
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
        """Strip governance-internal fields from messages before API call.

        Uses ``to_dict()`` (not ``model_dump()``) so that ``tool_calls``
        serializes to the OpenAI wire format (``id``/``type``/``function``)
        and ``arguments`` becomes a JSON string — ``model_dump()`` would
        produce the internal ToolCall field names instead.

        Coerces dict entries to ``ChatMessage`` at the trust boundary —
        callers may pass ``list[dict]`` (legacy tests, external callers),
        but the API serialization path requires ``ChatMessage.to_dict()``.

        ``reasoning_content`` is re-attached on assistant tool-call turns
        only, per the DeepSeek thinking-mode passback rule.
        """
        allowed = OpenAIProvider._API_MSG_FIELDS
        result: list[dict[str, Any]] = []
        for msg in messages:
            msg = ChatMessage.coerce(msg)
            raw = msg.to_dict()
            entry = {k: v for k, v in raw.items() if k in allowed}
            # reasoning_content is filtered out by the allowed-fields whitelist
            # above, so read it from the model extras; DeepSeek thinking-mode
            # passback requires it only on tool-call turns — other turns drop it.
            reasoning = msg.model_extra.get("reasoning_content") if msg.model_extra else None
            if reasoning and entry.get("role") == "assistant" and entry.get("tool_calls"):
                entry["reasoning_content"] = reasoning
            result.append(entry)
        return result

    def _build_params(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        """Build API params. temperature=None falls back to the constructor/config value."""
        params: dict[str, Any] = {
            "model": model or self._model,
            "messages": self._sanitize_api_messages(messages),
            "temperature": temperature if temperature is not None else self._temperature,
            "top_p": self._top_p,
            "max_tokens": max_output_tokens if max_output_tokens is not None else self._max_output_tokens,
            "stream": stream,
        }

        inject_reasoning_effort(params, self._reasoning_effort)

        session_id = kwargs.pop("prompt_cache_key", "")
        inject_cache_control(params, session_id)

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if stream:
            params["stream_options"] = {"include_usage": True}

        if self._extra_headers:
            params["extra_headers"] = self._extra_headers

        params.update(kwargs)
        return params
