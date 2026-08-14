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
    from modex_agent.core.types import LLMResponse
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.prompt_capture import PromptCaptureStrategy
    from modex_agent.trace.score_injector import L2ScoreInjector
    from modex_agent.trace.session_state import TraceSessionState


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
    ) -> None:
        super().__init__(
            session=session,
            store=store,
            model=model,
            provider_name=provider_name,
            request_params=request_params,
            score_injector=score_injector,
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
        self._session.llm_request_attrs[trace_id] = self._prompt_capture.capture(
            request,
            self._model,
            tools=tools,
            system_prompt=ctx.system_prompt or None,
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
            output_parts.extend(
                {
                    "type": "tool_call",
                    "name": tool_call.tool_name,
                    "arguments": json.dumps(
                        tool_call.arguments,
                        ensure_ascii=False,
                        default=str,
                    ),
                }
                for tool_call in response.tool_calls
            )
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
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        usage_attributes = (
            ("reasoning_tokens", GenAiAttr.USAGE_REASONING_TOKENS),
            ("cache_read_input_tokens", GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS),
            (
                "cache_creation_input_tokens",
                GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS,
            ),
        )
        if input_tokens is not None:
            attributes[GenAiAttr.USAGE_INPUT_TOKENS] = input_tokens
        if output_tokens is not None:
            attributes[GenAiAttr.USAGE_OUTPUT_TOKENS] = output_tokens
        for usage_key, attribute in usage_attributes:
            if usage_key in usage:
                attributes[attribute] = usage[usage_key]

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
        if input_tokens is not None:
            turn_usage["input_tokens"] += input_tokens
        if output_tokens is not None:
            turn_usage["output_tokens"] += output_tokens
        for usage_key, _ in usage_attributes:
            if usage_key in usage:
                turn_usage[usage_key] += usage[usage_key]

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
