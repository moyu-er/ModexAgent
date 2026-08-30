from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING

from modex_agent.hook.abc import StartNodeTurnHook
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.base_hook import BaseTraceHook
from modex_agent.trace.prompt_capture import OffPromptCapture
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
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.prompt_capture import PromptCaptureStrategy
    from modex_agent.trace.score_injector import L2ScoreInjector
    from modex_agent.trace.session_state import TraceSessionState


class AgentStartSpanHook(BaseTraceHook, StartNodeTurnHook):
    def __init__(
        self,
        session: TraceSessionState,
        store: OtelSpanTraceStore | None,
        model: str | None,
        provider_name: str | None,
        request_params: dict[str, object] | None,
        score_injector: L2ScoreInjector | None,
        prompt_capture: PromptCaptureStrategy,
        capture_tools: bool,
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
        self._capture_tools = capture_tools

    async def start_node_turn(self, ctx: AgentContext) -> None:
        if ctx.runtime is None:
            return
        trace_value = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        root_value = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if trace_value is None or root_value is None:
            return

        trace_id = str(trace_value)
        root_span_id = str(root_value)
        attributes = self._build_base_attrs(ctx, SpanName.AGENT_START.value)
        attributes[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] = LangfuseObservationType.SPAN.value

        if type(self._prompt_capture) is not OffPromptCapture:
            system_prompt = await ctx.get_resolved_system_prompt()
            attributes[GenAiAttr.SYSTEM_INSTRUCTIONS] = system_prompt
            attributes[GenAiAttr.LANGFUSE_OBS_METADATA_SYSTEM_PROMPT] = system_prompt
            attributes[GenAiAttr.SYSTEM_PROMPT_HASH] = hashlib.sha256(
                system_prompt.encode("utf-8")
            ).hexdigest()[:16]
            attributes[GenAiAttr.SYSTEM_PROMPT_LENGTH] = len(system_prompt)

        if self._capture_tools and ctx.tool_manager is not None:
            import json

            tools = ctx.get_tool_descriptions()
            attributes[GenAiAttr.REQUEST_TOOLS] = tools
            attributes[GenAiAttr.LANGFUSE_OBS_METADATA_TOOL_DEFINITIONS] = json.dumps(
                tools, ensure_ascii=False
            )

        now = time.time()
        await self._save_span(
            trace_id=trace_id,
            span_id=self._new_span_id(),
            parent_span_id=root_span_id,
            name=SpanName.AGENT_START.value,
            kind=SpanKind.INTERNAL.value,
            start_time=now,
            end_time=now,
            attributes=attributes,
            status=SpanStatus(code=SpanStatusCode.OK),
            ctx=ctx,
        )
