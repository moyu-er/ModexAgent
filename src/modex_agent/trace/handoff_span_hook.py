from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from modex_agent.hook.abc import AfterToolExecutionHook
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

_DISPATCH_TOOL_NAMES: Final[frozenset[str]] = frozenset({"send_to_agent", "task"})
_INVOCATION_ID_PATTERN: Final = re.compile(r"(?m)^invocation_id:\s*(\S+)\s*$")
_TARGET_AGENT_PATTERN: Final = re.compile(r"(?:to|at) ['\"]([^'\"]+)['\"]")


class HandoffSpanHook(BaseTraceHook, AfterToolExecutionHook):
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
        if batch_info is not None:
            batch_start, batch_span_id, tool_calls = batch_info
        else:
            batch_start = time.time()
            batch_span_id = str(root_value)
            tool_calls = ()

        dispatch_results = list(results)
        arguments_by_call_id: dict[str, dict[str, Any]] = {}
        calls_by_id = {
            tool_call.call_id: tool_call
            for tool_call in tool_calls
            if tool_call.call_id is not None
        }
        calls_list = list(tool_calls)

        for index, result in enumerate(dispatch_results):
            tool_call = calls_by_id.get(result.call_id) if result.call_id is not None else None
            if tool_call is None and index < len(calls_list):
                tool_call = calls_list[index]
            tool_name = tool_call.tool_name if tool_call is not None else result.tool_name
            if tool_name not in _DISPATCH_TOOL_NAMES:
                continue

            if tool_call is not None:
                arguments = tool_call.arguments
            elif result.call_id is not None:
                arguments = arguments_by_call_id.get(result.call_id, {})
            else:
                arguments = {}
            result_text = result.message_content()
            target_match = _TARGET_AGENT_PATTERN.search(result_text)
            target_agent = str(arguments.get("target_agent", ""))
            if not target_agent and target_match is not None:
                target_agent = target_match.group(1)
            target_hint = str(
                arguments.get(
                    "target_kind",
                    arguments.get("execution_strategy", ""),
                )
            ).lower()
            target_kind = "external" if target_hint == "external" else "subagent"
            invocation_match = _INVOCATION_ID_PATTERN.search(result_text)
            child_turn_id = invocation_match.group(1) if invocation_match is not None else ""
            parent_turn_id = ctx.identity.turn_id if ctx.identity is not None else ""
            message_type = "task_request" if result.success else "agent_result"
            handoff_span_id = self._new_span_id()
            attributes = self._build_base_attrs(ctx, SpanName.AGENT_HANDOFF.value)
            attributes.update(
                {
                    GenAiAttr.HANDOFF_TARGET_AGENT: target_agent,
                    GenAiAttr.HANDOFF_TARGET_KIND: target_kind,
                    GenAiAttr.HANDOFF_MESSAGE_TYPE: message_type,
                    GenAiAttr.HANDOFF_PARENT_TURN_ID: parent_turn_id,
                    GenAiAttr.HANDOFF_CHILD_TURN_ID: child_turn_id,
                    GenAiAttr.HANDOFF_CHILD_TRACE_ID: trace_id,
                    GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.SPAN.value,
                }
            )
            await self._save_span(
                trace_id=trace_id,
                span_id=handoff_span_id,
                parent_span_id=batch_span_id,
                name=SpanName.AGENT_HANDOFF.value,
                kind=SpanKind.INTERNAL.value,
                start_time=batch_start,
                end_time=time.time(),
                attributes=attributes,
                status=SpanStatus(
                    code=SpanStatusCode.OK if result.success else SpanStatusCode.ERROR,
                    message=result.error,
                ),
                ctx=ctx,
            )
            ctx.runtime.state.custom[TurnCustomKey.HANDOFF_SPAN_ID] = handoff_span_id
