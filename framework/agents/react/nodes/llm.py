"""LLMNode — assembles messages, calls LLM, writes assistant message."""
from __future__ import annotations

from typing import TYPE_CHECKING

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.state import get_react_state
from framework.control.runtime import ControlPhase
from framework.core.agent import AgentContext
from framework.core.constants import FinishReason
from framework.core.graph.node import Node, NodeTransition
from framework.core.provider import StreamingLLMProvider
from framework.core.types import LLMResponse
from framework.hook import HookPayload, HookPoint
from framework.interceptor.abc import InterceptorScope, IterationContext
from framework.runtime.enums import MessageDeltaSource, OperationKind, TurnPhase
from framework.runtime.models import MessageDelta

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class LLMNode(Node):
    """Calls LLM, writes assistant message, routes to ToolNode or EndNode."""

    def __init__(self, agent: ReActAgent) -> None:
        super().__init__(ReActNode.LLM)
        self._agent = agent

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        state = get_react_state(ctx)
        if state is None:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        state.iteration += 1
        state.current_node = ReActNode.LLM
        state.phase = TurnPhase.RUNNING

        if state.iteration > ctx.max_iterations:
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        runtime = ctx.runtime

        async def actual_iteration():
            if ctx.emitter is not None:
                await ctx.emitter.emit(
                    ReActEvent.ITERATION_START, {"iteration": state.iteration},
                )

            if runtime and runtime.hooks:
                await runtime.hooks.dispatch(HookPoint.BEFORE_ITERATION, ctx)
            if runtime and runtime.injection_queue:
                await self._agent._drain_injections(ctx)

            messages = await self._build_messages(ctx)
            response = await self._call_llm(messages, ctx)

            if runtime and runtime.hooks:
                await runtime.hooks.dispatch(
                    HookPoint.AFTER_LLM_RESPONSE, ctx,
                    payload=HookPayload(data={"response": response}),
                )

            if response.finish_reason == FinishReason.ERROR.value:
                state.llm_response = response
                return

            from framework.utils.helpers import strip_think
            from framework.utils.message_builder import build_assistant_message

            # If the provider did NOT separate reasoning_content (non-standard API),
            # sanitize possible <think> tags embedded in content.
            content = response.content or ""
            if response.reasoning_content is None:
                content = strip_think(content) or ""

            assistant_msg = build_assistant_message(
                content, response.tool_calls, response.reasoning_content,
            )
            await ctx.history.append(assistant_msg)
            state.llm_response = response
            state.add_operation(OperationKind.LLM_CALL, None)
            state.message_delta.append(
                MessageDelta(message=assistant_msg, source=MessageDeltaSource.ASSISTANT)
            )

        if (
            runtime and runtime.interceptors
            and runtime.interceptors.has_scope(InterceptorScope.ITERATION)
        ):
            await runtime.interceptors.around_iteration(
                ctx,
                IterationContext(iteration=state.iteration, turn_id=ctx.session_id),
                actual_iteration,
            )
        else:
            await actual_iteration()

        response = state.llm_response
        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        if response is not None and response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.ITERATION_END, {
                "iteration": state.iteration, "has_tool_calls": False,
            })
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)

    async def _build_messages(self, ctx: AgentContext) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages.extend(await ctx.to_messages())

        governance = ctx.runtime.governance if ctx.runtime else None
        if governance is not None:
            messages = await governance.apply(messages)
        return messages

    async def _call_llm(
        self, messages: list[dict[str, object]], ctx: AgentContext,
    ) -> LLMResponse:
        if ctx.runtime and ctx.runtime.control:
            await ctx.runtime.control.drain(ctx, phase=ControlPhase.BEFORE_LLM)
        emitter = ctx.emitter
        if emitter is not None and emitter.wants_streaming() and isinstance(
            self._agent.provider, StreamingLLMProvider,
        ):
            interceptor_chain = ctx.runtime.interceptors if ctx.runtime else None
            if interceptor_chain is not None:
                if interceptor_chain.has_scope(InterceptorScope.LLM_STREAM):
                    return await self._agent._stream_with_control(messages, ctx)
            return await self._stream_plain(messages, ctx)
        return await self._call_non_streaming(messages, ctx)

    async def _stream_plain(
        self, messages: list[dict[str, object]], ctx: AgentContext,
    ) -> LLMResponse:
        async def _on_content(delta: str) -> None:
            if delta and ctx.emitter is not None:
                await ctx.emitter.emit_delta(delta)
                await ctx.emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

        async def _on_reasoning(delta: str) -> None:
            if delta and ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.MODEL_REASONING, delta)

        response = await self._agent.provider.chat_stream(
            messages=messages,
            tools=ctx.get_tool_descriptions() if ctx.tool_manager else None,
            temperature=ctx.temperature or 0.7,
            max_tokens=ctx.max_tokens,
            on_content_delta=_on_content,
            on_reasoning_delta=_on_reasoning,
        )
        if ctx.emitter is not None:
            await ctx.emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response

    async def _call_non_streaming(
        self, messages: list[dict[str, object]], ctx: AgentContext,
    ) -> LLMResponse:
        response = await self._agent.provider.chat(
            messages=messages,
            tools=ctx.get_tool_descriptions() if ctx.tool_manager else None,
            temperature=ctx.temperature or 0.7,
            max_tokens=ctx.max_tokens,
        )
        if ctx.emitter is not None:
            if response.content:
                await ctx.emitter.emit_content(response.content)
                await ctx.emitter.emit(ReActEvent.MODEL_OUTPUT, response.content)
            if response.reasoning_content:
                await ctx.emitter.emit(ReActEvent.MODEL_REASONING, response.reasoning_content)
            await ctx.emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response
