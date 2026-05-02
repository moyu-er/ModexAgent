"""LLMNode — assembles messages, calls LLM, writes assistant message."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.agents.react.agent import ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.core.agent import AgentContext, ctx_ext
from framework.core.constants import FinishReason
from framework.core.context_extensions import ExtensionKey
from framework.core.graph.node import Node, NodeTransition
from framework.core.provider import StreamingLLMProvider
from framework.core.types import LLMResponse
from framework.hook import HookPoint

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent


class LLMNode(Node):
    """Calls LLM, writes assistant message, routes to ToolNode or EndNode."""

    def __init__(self, agent: ReActAgent, *, enable_hooks: bool = True) -> None:
        super().__init__(ReActNode.LLM)
        self._agent = agent
        self._enable_hooks = enable_hooks

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        iteration = ctx.metadata[ReActMetaKey.ITERATION] + 1
        ctx.metadata[ReActMetaKey.ITERATION] = iteration

        if iteration > ctx.max_iterations:
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.BEFORE_ITERATION, ctx)
            await self._agent._drain_injections(ctx)

        messages = await self._build_messages(ctx)
        response = await self._call_llm(messages, ctx)

        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.AFTER_LLM_RESPONSE, ctx, response)

        if response.finish_reason == FinishReason.ERROR.value:
            ctx.metadata[ReActMetaKey.LLM_RESPONSE] = response
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        assistant_msg = self._agent._build_assistant_message(
            response.content or "", response.tool_calls,
        )
        await ctx.history.append(assistant_msg)
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = response
        msgs: list = ctx.metadata.setdefault(ReActMetaKey.ITERATION_MSGS, [])
        msgs.append(assistant_msg)
        await self._agent._save_checkpoint(msgs, ctx)

        if response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)

        if ctx.emitter is not None:
            await ctx.emitter.emit(ReActEvent.ITERATION_END, {
                "iteration": iteration, "has_tool_calls": False,
            })
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)

    async def _build_messages(self, ctx: AgentContext) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages.extend(await ctx.to_messages())

        governance = ctx_ext(ctx, ExtensionKey.GOVERNANCE)
        if governance is not None:
            messages = await governance.apply(messages)
        return messages

    async def _call_llm(
        self, messages: list[dict[str, Any]], ctx: AgentContext,
    ) -> LLMResponse:
        emitter = ctx.emitter
        if emitter is not None and emitter.wants_streaming() and isinstance(
            self._agent.provider, StreamingLLMProvider,
        ):
            interceptor_chain = ctx_ext(ctx, ExtensionKey.INTERCEPTOR_CHAIN)
            if interceptor_chain is not None:
                from framework.interceptor.abc import InterceptorScope
                if interceptor_chain.has_scope(InterceptorScope.LLM_STREAM):
                    return await self._agent._stream_with_control(
                        messages, ctx,
                    )
            return await self._stream_plain(messages, ctx)
        return await self._call_non_streaming(messages, ctx)

    async def _stream_plain(
        self, messages: list[dict[str, Any]], ctx: AgentContext,
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
        self, messages: list[dict[str, Any]], ctx: AgentContext,
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
