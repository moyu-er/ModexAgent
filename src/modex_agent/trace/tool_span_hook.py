from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from modex_agent.core.types import ToolCall
from modex_agent.hook.abc import AfterToolExecutionHook, BeforeToolExecutionHook
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
    from modex_agent.core.tool_manager import ToolResult

_TOOL_COUNT_ATTR: Final = "gen_ai.tool.count"
_TOOL_NAMES_ATTR: Final = "gen_ai.tool.names"
_TOOL_EXECUTION_TIME_ATTR: Final = "gen_ai.tool.execution_time"


class ToolSpanHook(BaseTraceHook, BeforeToolExecutionHook, AfterToolExecutionHook):
    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: Sequence[ToolCall],
    ) -> None:
        if ctx.runtime is None:
            return
        trace_value = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        batch_span_id = self._new_span_id()
        self._session.tool_batch_info[trace_id] = (
            time.time(),
            batch_span_id,
            tool_calls,
        )

    async def after_tool_execution(
        self,
        ctx: AgentContext,
        results: Sequence[ToolResult],
    ) -> None:
        if ctx.runtime is None:
            return
        trace_value = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        batch_info = self._session.tool_batch_info.get(trace_id)
        if batch_info is None:
            return
        batch_start, batch_span_id, tool_calls = batch_info
        if not results:
            self._session.tool_batch_info.pop(trace_id)
            return

        root_span_id = str(root_value)
        end_time = time.time()
        batch_error = next((result.error for result in results if result.error is not None), None)
        batch_attributes = self._build_base_attrs(ctx, SpanName.EXECUTE_TOOL_BATCH.value)
        batch_attributes.update(
            {
                _TOOL_COUNT_ATTR: len(results),
                _TOOL_NAMES_ATTR: [result.tool_name for result in results],
                GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.SPAN.value,
            }
        )
        await self._save_span(
            trace_id=trace_id,
            span_id=batch_span_id,
            parent_span_id=root_span_id,
            name=SpanName.EXECUTE_TOOL_BATCH.value,
            kind=SpanKind.INTERNAL.value,
            start_time=batch_start,
            end_time=end_time,
            attributes=batch_attributes,
            status=SpanStatus(
                code=SpanStatusCode.ERROR if batch_error is not None else SpanStatusCode.OK,
                message=batch_error,
            ),
            ctx=ctx,
        )

        calls_by_id = {
            tool_call.call_id: tool_call
            for tool_call in tool_calls
            if tool_call.call_id is not None
        }
        calls_list = list(tool_calls)
        for index, result in enumerate(results):
            tool_call = calls_by_id.get(result.call_id) if result.call_id is not None else None
            if tool_call is None and index < len(calls_list):
                tool_call = calls_list[index]
            # ToolNode stamps the canonical ToolCall id onto every result
            # path; the tool_call fallback keeps the attribute non-empty
            # even on paths that bypass ToolNode's stamping.
            call_id = result.call_id or (tool_call.call_id if tool_call is not None else None)
            arguments = tool_call.arguments if tool_call is not None else {}
            result_text = result.message_content()
            attributes = self._build_base_attrs(ctx, SpanName.EXECUTE_TOOL.value)
            attributes.update(
                {
                    GenAiAttr.TOOL_NAME: result.tool_name,
                    GenAiAttr.TOOL_TYPE: "function",
                    GenAiAttr.TOOL_CALL_ID: call_id or "",
                    GenAiAttr.TOOL_CALL_ARGUMENTS: json.dumps(
                        arguments,
                        ensure_ascii=False,
                        default=str,
                    ),
                    GenAiAttr.TOOL_RESULT: result_text,
                    GenAiAttr.TOOL_SUCCESS: result.success,
                    GenAiAttr.TOOL_FAIL: not result.success,
                    _TOOL_EXECUTION_TIME_ATTR: result.execution_time,
                    GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.TOOL.value,
                    GenAiAttr.LANGFUSE_OBSERVATION_INPUT: json.dumps(
                        {"tool_name": result.tool_name, "arguments": arguments},
                        ensure_ascii=False,
                        default=str,
                    ),
                    GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT: json.dumps(
                        {"result": result_text},
                        ensure_ascii=False,
                        default=str,
                    ),
                }
            )
            if result.error is not None:
                attributes[GenAiAttr.TOOL_ERROR_TYPE] = result.error
            await self._save_span(
                trace_id=trace_id,
                span_id=self._new_span_id(),
                parent_span_id=batch_span_id,
                name=SpanName.EXECUTE_TOOL.value,
                kind=SpanKind.INTERNAL.value,
                start_time=batch_start,
                end_time=end_time,
                attributes=attributes,
                status=SpanStatus(
                    code=SpanStatusCode.OK if result.success else SpanStatusCode.ERROR,
                    message=result.error,
                ),
                ctx=ctx,
            )
