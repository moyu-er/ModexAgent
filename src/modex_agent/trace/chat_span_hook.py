from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from modex_agent.hook.abc import AfterLLMResponseHook, BeforeLLMHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
)
from modex_agent.trace.store import SpanStatus

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.types import LLMResponse, ToolCall
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.prompt_capture import PromptCaptureStrategy
    from modex_agent.trace.score_injector import L2ScoreInjector
    from modex_agent.trace.session_state import TraceSessionState


def _tool_call_parts(tool_calls: Sequence[ToolCall]) -> list[dict[str, object]]:
    """Render tool calls as OTel parts-based ``tool_call`` parts.

    Mirrors the input-capture part shape (``prompt_capture._capture_message_parts``):
    ``{"type": "tool_call", "id": ..., "name": ..., "arguments": ...}``. The
    ``id`` is the canonical call id (LLMNode stamps it before this hook
    fires); a ``None`` id omits the key rather than emitting JSON ``null``,
    which OTLP attributes cannot carry.
    """
    parts: list[dict[str, object]] = []
    for tool_call in tool_calls:
        part: dict[str, object] = {
            "type": "tool_call",
            "name": tool_call.tool_name,
            "arguments": json.dumps(
                tool_call.arguments,
                ensure_ascii=False,
                default=str,
            ),
        }
        if tool_call.call_id is not None:
            part["id"] = tool_call.call_id
        parts.append(part)
    return parts


class ChatSpanHook(BaseTraceHook, BeforeLLMHook, AfterLLMResponseHook):
    def __init__(
        self,
        session: TraceSessionState,
        store: OtelSpanTraceStore | None,
        model: str | None,
        provider_name: str | None,
        request_params: dict[str, object] | None,
        score_injector: L2ScoreInjector | None,
        prompt_capture: PromptCaptureStrategy,
        environment: str = "default",
        version: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        super().__init__(
            session=session,
            store=store,
            model=model,
            provider_name=provider_name,
            request_params=request_params,
            score_injector=score_injector,
            environment=environment,
            version=version,
            tags=tags,
        )
        self._prompt_capture = prompt_capture

    async def before_llm(self, ctx: AgentContext, request: Sequence[ChatMessage]) -> None:
        if ctx.runtime is None:
            return
        trace_value = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        if trace_value is None:
            return

        trace_id = str(trace_value)
        self._session.llm_start_times[trace_id] = time.time()
        tools = ctx.get_tool_descriptions() if ctx.tool_manager else None
        resolved_system_prompt = await ctx.get_resolved_system_prompt()
        self._session.llm_request_attrs[trace_id] = self._prompt_capture.capture(
            request,
            self._model,
            tools=tools,
            system_prompt=resolved_system_prompt or None,
        )

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        if ctx.runtime is None:
            return
        trace_value = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        root_span_id = str(root_value)
        start_time = self._session.llm_start_times[trace_id]
        request_attributes = self._session.llm_request_attrs[trace_id]
        end_time = time.time()
        response_content = response.content or ""
        output_parts: list[dict[str, object]] = [{"type": "text", "content": response_content}]
        if response.tool_calls:
            output_parts.extend(_tool_call_parts(response.tool_calls))
        output_messages: list[dict[str, object]] = [{"role": "assistant", "parts": output_parts}]

        attributes = self._build_base_attrs(ctx, SpanName.CHAT.value)
        attributes.update(request_attributes)
        attributes.update(
            {
                GenAiAttr.RESPONSE_FINISH_REASONS: [response.finish_reason.value],
                "has_tool_calls": response.has_tool_calls,
                GenAiAttr.OUTPUT_MESSAGES: output_messages,
                GenAiAttr.GEN_AI_COMPLETION: response_content,
                GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.GENERATION.value,
                GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT: json.dumps(
                    output_messages,
                    ensure_ascii=False,
                    default=str,
                ),
                GenAiAttr.API_DURATION_S: end_time - start_time,
            }
        )

        input_messages = request_attributes.get(GenAiAttr.INPUT_MESSAGES)
        if input_messages is not None:
            serialized_input = json.dumps(input_messages, ensure_ascii=False, default=str)
            attributes[GenAiAttr.GEN_AI_PROMPT] = serialized_input
            attributes[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] = serialized_input

        if self._model is not None:
            attributes.setdefault(GenAiAttr.REQUEST_MODEL, self._model)
            attributes[GenAiAttr.RESPONSE_MODEL] = self._model
        if response.response_id is not None:
            attributes[GenAiAttr.RESPONSE_ID] = response.response_id
        if self._request_params is not None:
            request_parameter_attributes = (
                ("temperature", GenAiAttr.REQUEST_TEMPERATURE),
                ("max_tokens", GenAiAttr.REQUEST_MAX_TOKENS),
                ("top_p", GenAiAttr.REQUEST_TOP_P),
                ("stream", GenAiAttr.REQUEST_STREAM),
            )
            for parameter, attribute in request_parameter_attributes:
                if parameter in self._request_params:
                    attributes[attribute] = self._request_params[parameter]

        if response.reasoning_content:
            attributes[GenAiAttr.OUTPUT_REASONING_CONTENT] = response.reasoning_content
        if response.completion_start_time is not None:
            attributes[GenAiAttr.LANGFUSE_OBSERVATION_COMPLETION_START_TIME] = (
                response.completion_start_time
            )
        if response.tool_calls:
            attributes[GenAiAttr.OUTPUT_TOOL_CALLS] = [
                {
                    "call_id": tool_call.call_id,
                    "tool_name": tool_call.tool_name,
                    "arguments": json.dumps(
                        tool_call.arguments,
                        ensure_ascii=False,
                        default=str,
                    ),
                }
                for tool_call in response.tool_calls
            ]

        usage = response.usage
        for value, attribute in (
            (usage.input_tokens, GenAiAttr.USAGE_INPUT_TOKENS),
            (usage.output_tokens, GenAiAttr.USAGE_OUTPUT_TOKENS),
            (usage.reasoning_tokens, GenAiAttr.USAGE_REASONING_TOKENS),
            (usage.cache_read_input_tokens, GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS),
            (usage.cache_creation_input_tokens, GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS),
        ):
            if value:
                attributes[attribute] = value

        turn_usage = self._session.turn_usage.setdefault(
            trace_id,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "reasoning_tokens": 0,
            },
        )
        turn_usage["input_tokens"] += usage.input_tokens
        turn_usage["output_tokens"] += usage.output_tokens
        turn_usage["cache_read_input_tokens"] += usage.cache_read_input_tokens
        turn_usage["cache_creation_input_tokens"] += usage.cache_creation_input_tokens
        turn_usage["reasoning_tokens"] += usage.reasoning_tokens

        failed = response.error is not None
        await self._save_span(
            trace_id=trace_id,
            span_id=self._new_span_id(),
            parent_span_id=root_span_id,
            name=SpanName.CHAT.value,
            kind=SpanKind.CLIENT.value,
            start_time=start_time,
            end_time=end_time,
            attributes=attributes,
            status=SpanStatus(
                code=SpanStatusCode.ERROR if failed else SpanStatusCode.OK,
                message=response.error,
            ),
            ctx=ctx,
        )
        self._session.llm_start_times.pop(trace_id)
        self._session.llm_request_attrs.pop(trace_id)
