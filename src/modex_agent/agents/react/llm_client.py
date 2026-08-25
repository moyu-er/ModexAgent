"""ReactLlmClient — the ReAct agent's LLM caller.

Single entry call() picks the path from ctx: control-draining stream (when an
LLM_STREAM-scope interceptor chain is present), plain stream, or non-streaming.
Absorbs ReActAgent._stream_with_control + LLMNode._call_llm/_stream_plain/
_call_non_streaming so nodes hold a collaborator, not an agent back-reference.

The control-drain path preserves the INTERRUPTED_PARTIAL contract: on mid-stream
interrupt it stashes the live partial into turn state, which the agent's run()
cancel/error handler persists.
"""
from __future__ import annotations

import asyncio
import logging

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.error_recovery import (
    ErrorRecoveryConfig,
    attempt_recovery,
    is_context_overflow_error,
)
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider, StreamingLLMProvider
from modex_agent.core.types import LLMResponse, ToolCall
from modex_agent.interceptor.abc import (
    InterceptorScope,
    LLMStreamChunk,
    LLMStreamContext,
)
from modex_agent.runtime.dispatch import renew_dispatch_deadline
from modex_agent.runtime.enums import TurnCustomKey

logger = logging.getLogger(__name__)


class ReactLlmClient:
    """Produce an LLMResponse from messages, picking stream/non-stream/control-drain internally."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def call(
        self,
        messages: list[dict[str, object]],
        ctx: AgentContext,
    ) -> LLMResponse:
        # B6 boundary: LLMNode._build_messages (and the to_messages /
        # governance pipeline) still produces list[dict]; coerce to
        # list[ChatMessage] so the provider ABC and LLMStreamContext
        # receive typed structs.
        typed_messages = [ChatMessage.coerce(m) for m in messages]
        emitter = ctx.emitter
        use_streaming = (
            emitter is not None
            and emitter.wants_streaming()
            and isinstance(self._provider, StreamingLLMProvider)
        )
        if use_streaming:
            interceptor_chain = ctx.runtime.interceptors if ctx.runtime else None
            if (
                interceptor_chain is not None
                and interceptor_chain.has_scope(InterceptorScope.LLM_STREAM)
            ):
                return await self._stream_with_control(typed_messages, ctx)
            return await self._stream_with_recovery(typed_messages, ctx)
        return await self._call_non_streaming_with_recovery(typed_messages, ctx)

    async def _stream_with_control(
        self,
        messages: list[ChatMessage],
        context: AgentContext,
    ) -> LLMResponse:
        """LLM streaming call wrapped by the InterceptorChain."""
        assert isinstance(self._provider, StreamingLLMProvider)
        emitter = context.emitter

        stream_ctx = LLMStreamContext(
            messages=messages,
            model=self._provider.get_default_model(),
            session_id=str(context.session),
        )

        accumulated_content = ""
        accumulated_reasoning = ""
        # Partial content streamed via _on_content_delta. Unlike
        # accumulated_content (which only fills from the final end-of-stream
        # chunk), this tracks content live during streaming — so it holds the
        # partial text when a cancel interrupts mid-stream (the final chunk
        # never arrives). Used to persist an interrupted assistant message.
        streamed_content = ""
        finish_reason: FinishReason = FinishReason.STOP
        tool_calls_list: list[ToolCall] = []
        provider_response: LLMResponse | None = None

        async def _actual_stream():
            """Call provider.chat_stream, converting the result to LLMStreamChunk."""
            nonlocal tool_calls_list, provider_response

            async def _on_content_delta(delta: str) -> None:
                nonlocal streamed_content
                # Drain control channel between every content delta so a
                # CANCEL_TURN arriving mid-stream interrupts the LLM
                # immediately (inside the provider's streaming loop, before
                # the next chunk). Without this, cancel only takes effect
                # after the full chat_stream returns.
                if context.runtime and context.runtime.control_channel:
                    from modex_agent.hook.builtin.control_drain import drain_control_channel
                    await drain_control_channel(
                        context.runtime.control_channel,
                        context,
                        turn_uuid=context.runtime.turn_uuid,
                    )
                renew_dispatch_deadline()
                if delta:
                    streamed_content += delta
                    await emitter.emit_delta(delta)
                    await emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

            async def _on_reasoning_delta(delta: str) -> None:
                if context.runtime and context.runtime.control_channel:
                    from modex_agent.hook.builtin.control_drain import drain_control_channel
                    await drain_control_channel(
                        context.runtime.control_channel,
                        context,
                        turn_uuid=context.runtime.turn_uuid,
                    )
                renew_dispatch_deadline()
                if delta:
                    await emitter.emit(ReActEvent.MODEL_REASONING, delta)

            # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
            # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
            # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
            # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。
            response = await self._provider.chat_stream(
                messages=messages,
                tools=context.get_tool_descriptions() if context.tool_manager else None,
                temperature=context.temperature,
                max_output_tokens=context.max_output_tokens,
                on_content_delta=_on_content_delta,
                on_reasoning_delta=_on_reasoning_delta,
                prompt_cache_key=str(context.session),
            )
            provider_response = response
            tool_calls_list = list(response.tool_calls or [])
            yield LLMStreamChunk(
                content_delta=response.content,
                reasoning_delta=response.reasoning_content,
                finish_reason=response.finish_reason,
            )

        interceptor_chain = context.runtime.interceptors if context.runtime else None
        try:
            # Canonical AOP path for LLM_STREAM. `ctx.runtime.around` is for
            # ITERATION only — see ADR-0033 D5.
            async for chunk in interceptor_chain.around_llm_stream(
                context,
                stream_ctx,
                _actual_stream,
            ):
                if chunk.control_action == "cancel":
                    finish_reason = chunk.finish_reason or FinishReason.CANCELLED
                    logger.warning(
                        "LLM stream cancelled session=%s finish_reason=%s",
                        str(context.session),
                        finish_reason,
                    )
                    break
                if chunk.content_delta:
                    accumulated_content += chunk.content_delta
                if chunk.reasoning_delta:
                    accumulated_reasoning += chunk.reasoning_delta
                if chunk.finish_reason:
                    finish_reason = chunk.finish_reason
        except (asyncio.CancelledError, Exception):
            # Stream interrupted mid-flight (user /stop, pause, timeout, error).
            # The normal assistant-message append (llm node ctx.history.append)
            # never runs, so memory would lose this partial content. Stash the
            # live-streamed partial (streamed_content) for the agent's
            # cancel/error handler to persist as an XML-marked interrupted
            # message, keeping memory aligned with the transcript.
            if streamed_content or tool_calls_list:
                state = get_react_state(context)
                if state is not None:
                    state.custom[TurnCustomKey.INTERRUPTED_PARTIAL] = {
                        "content": streamed_content,
                        "tool_names": [tc.tool_name for tc in tool_calls_list],
                    }
            raise

        has_tool_calls = bool(tool_calls_list)
        await emitter.emit_stream_end(resuming=has_tool_calls)
        return LLMResponse(
            content=accumulated_content or None,
            reasoning_content=accumulated_reasoning or None,
            finish_reason=finish_reason,
            tool_calls=tool_calls_list,
            usage=provider_response.usage if provider_response is not None else {},
            completion_start_time=(
                provider_response.completion_start_time
                if provider_response is not None
                else None
            ),
        )

    async def _call_non_streaming_with_recovery(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        """Non-streaming path wrapped in a context-overflow recovery loop."""
        config = ErrorRecoveryConfig()
        current_messages: list[ChatMessage] = list(messages)
        for attempt in range(config.max_context_overflow_retries + 1):
            try:
                return await self._call_non_streaming(current_messages, ctx)
            except Exception as e:
                if not is_context_overflow_error(e):
                    raise
                recovery = await attempt_recovery(current_messages, e, attempt, config, ctx)
                if not recovery.should_retry:
                    raise
                logger.warning("LLM context overflow, retrying: %s", recovery.reason)
                if recovery.trimmed_messages is not None:
                    current_messages = recovery.trimmed_messages
        # Should not reach here, but just in case
        return await self._call_non_streaming(current_messages, ctx)

    async def _stream_with_recovery(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        """Plain streaming path wrapped in a context-overflow recovery loop.

        Only catches errors raised *before* any delta is emitted (which is when
        413/context-overflow occurs — the initial request is too large).
        """
        config = ErrorRecoveryConfig()
        current_messages: list[ChatMessage] = list(messages)
        for attempt in range(config.max_context_overflow_retries + 1):
            try:
                return await self._stream_plain(current_messages, ctx)
            except Exception as e:
                if not is_context_overflow_error(e):
                    raise
                recovery = await attempt_recovery(current_messages, e, attempt, config, ctx)
                if not recovery.should_retry:
                    raise
                logger.warning(
                    "LLM context overflow during streaming, retrying: %s",
                    recovery.reason,
                )
                if recovery.trimmed_messages is not None:
                    current_messages = recovery.trimmed_messages
        return await self._stream_plain(current_messages, ctx)

    async def _stream_plain(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        assert isinstance(self._provider, StreamingLLMProvider)

        async def _on_content(delta: str) -> None:
            renew_dispatch_deadline()
            if delta and ctx.emitter is not None:
                await ctx.emitter.emit_delta(delta)
                await ctx.emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

        async def _on_reasoning(delta: str) -> None:
            renew_dispatch_deadline()
            if delta and ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.MODEL_REASONING, delta)

        # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
        # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
        # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
        # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。
        response = await self._provider.chat_stream(
            messages=messages,
            tools=ctx.get_tool_descriptions() if ctx.tool_manager else None,
            temperature=ctx.temperature,
            max_output_tokens=ctx.max_output_tokens,
            on_content_delta=_on_content,
            on_reasoning_delta=_on_reasoning,
            prompt_cache_key=str(ctx.session),
        )
        if ctx.emitter is not None:
            await ctx.emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response

    async def _call_non_streaming(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
        # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
        # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
        # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。收敛目标见
        # docs/superpowers/plans/2026-07-03-bot-multi-model.md §框架配置收敛后续。
        response = await self._provider.chat(
            messages=messages,
            tools=ctx.get_tool_descriptions() if ctx.tool_manager else None,
            temperature=ctx.temperature,
            max_output_tokens=ctx.max_output_tokens,
            prompt_cache_key=str(ctx.session),
        )
        if ctx.emitter is not None:
            if response.content:
                await ctx.emitter.emit_content(response.content)
                await ctx.emitter.emit(ReActEvent.MODEL_OUTPUT, response.content)
            if response.reasoning_content:
                await ctx.emitter.emit(ReActEvent.MODEL_REASONING, response.reasoning_content)
            await ctx.emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response
