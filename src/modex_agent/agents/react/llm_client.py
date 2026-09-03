"""ReactLlmClient — the ReAct agent's LLM caller.

Single event loop (ADR-0046): every call is one pass over
``provider.stream(request)`` — stream-native providers yield real event
streams; callback-style providers (CallbackStreamProvider) go through the
callback→event bridge. The LLM_STREAM-scope interceptor chain wraps the
event iterator (events in, events out). Emitter driving happens at the
event dispatch point and is gated on ``emitter.wants_streaming()``;
non-streaming emitters ride the same loop without per-delta emits.

The loop preserves the INTERRUPTED_PARTIAL contract: on mid-stream interrupt
it stashes the live partial into turn state, which the agent's run()
cancel/error handler persists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.error_recovery import (
    ErrorRecoveryConfig,
    attempt_recovery,
    is_context_overflow_error,
)
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.stream_events import (
    EventAssembler,
    Finish,
    ReasoningDelta,
    TextDelta,
    ToolCallComplete,
)
from modex_agent.core.types import LLMResponse
from modex_agent.interceptor.abc import (
    InterceptorScope,
    LLMStreamContext,
    aclose_llm_stream,
)
from modex_agent.runtime.dispatch import renew_dispatch_deadline
from modex_agent.runtime.enums import TurnCustomKey

logger = logging.getLogger(__name__)


class ReactLlmClient:
    """Produce an LLMResponse from messages via a single LLMStreamEvent loop."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def call(
        self,
        messages: Sequence[dict[str, object] | ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        # B6 boundary: callers may still produce list[dict] (LLMNode now
        # passes list[ChatMessage] after inject_multimodal); coerce to
        # list[ChatMessage] so the provider ABC and LLMStreamContext
        # receive typed structs. Coercing a ChatMessage is a pass-through.
        typed_messages = [ChatMessage.coerce(m) for m in messages]
        # Emitter streaming preference is decided INSIDE the event loop (at
        # the dispatch point) so streaming and non-streaming emitters ride
        # the same path.
        return await self._stream_with_recovery(typed_messages, ctx)

    async def _stream_with_recovery(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        """Event-loop path wrapped in a context-overflow recovery loop.

        Catches errors raised out of the loop (stream-native providers whose
        ``stream()`` raises, emitter failures, control-drain cancels).
        Callback-style providers surface their failures as StreamFailure
        terminal events instead — those become ERROR responses, not
        exceptions, and do not enter this loop.
        """
        config = ErrorRecoveryConfig()
        current_messages: list[ChatMessage] = list(messages)
        for attempt in range(config.max_context_overflow_retries + 1):
            try:
                return await self._run_event_loop(current_messages, ctx)
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
        return await self._run_event_loop(current_messages, ctx)

    async def _run_event_loop(
        self,
        messages: list[ChatMessage],
        ctx: AgentContext,
    ) -> LLMResponse:
        """The single LLMStreamEvent loop (ADR-0046 PRD §10)."""
        emitter = ctx.emitter
        streaming_emitter = emitter if emitter is not None and emitter.wants_streaming() else None

        # TODO(model-config-convergence): 模型调用参数 temperature/max_output_tokens 应只由
        # LLMProvider 持有；此处经 descriptor/context 透传属冗余复制。待 ReactLlmClient
        # 不再传这两参后，本字段/参数可连同 AgentContext.temperature/max_output_tokens、
        # AgentLLMConfig、AgentMaterializeDeps 的同名字段一并删除。
        tools = ctx.get_tool_descriptions() if ctx.tool_manager else None
        request = LLMRequest(
            model=self._provider.get_default_model(),
            messages=messages,
            tools=tuple(tools or []),
            temperature=ctx.temperature,
            max_output_tokens=ctx.max_output_tokens,
            prompt_cache_key=str(ctx.session),
        )

        events = self._provider.stream(request)
        interceptor_chain = ctx.runtime.interceptors if ctx.runtime else None
        if interceptor_chain is not None and interceptor_chain.has_scope(
            InterceptorScope.LLM_STREAM
        ):
            # Canonical AOP path for LLM_STREAM. `ctx.runtime.around` is for
            # ITERATION only — see ADR-0033 D5. stream_ctx is interceptor
            # context only (the request envelope above is what goes on the
            # wire).
            stream_ctx = LLMStreamContext(
                messages=messages,
                model=self._provider.get_default_model(),
                session_id=str(ctx.session),
            )
            events = interceptor_chain.around_llm_stream(ctx, stream_ctx, events)

        assembler = EventAssembler()
        # Live partials for the INTERRUPTED_PARTIAL stash. Content arrives
        # event-by-event (the final response only exists after the terminal
        # event), so track them as events are dispatched — this holds the
        # partial text when a cancel interrupts mid-stream and the terminal
        # event never arrives.
        streamed_content = ""
        tool_names: list[str] = []
        try:
            async for event in events:
                match event:
                    case TextDelta():
                        await self._drain_control(ctx)
                        renew_dispatch_deadline()
                        if event.text:
                            streamed_content += event.text
                            if streaming_emitter is not None:
                                await streaming_emitter.emit_delta(event.text)
                                await streaming_emitter.emit(ReActEvent.MODEL_OUTPUT, event.text)
                        await assembler.feed(event)
                    case ReasoningDelta():
                        await self._drain_control(ctx)
                        renew_dispatch_deadline()
                        if streaming_emitter is not None and event.text:
                            await streaming_emitter.emit(ReActEvent.MODEL_REASONING, event.text)
                        await assembler.feed(event)
                    case ToolCallComplete():
                        tool_names.append(event.tool_name)
                        await assembler.feed(event)
                    case _:
                        await assembler.feed(event)
                # Bridge-translated cancellation must re-enter the lifecycle
                # as asyncio.CancelledError so agent.py's CancelledError
                # handler yields a CANCELLED AgentResult — the single
                # convergence point for cancel semantics. Native engines
                # never emit Finish(CANCELLED) (zero emitters under
                # providers/http/formats/; the ADR-0046 callback bridge is
                # the sole producer), so this check serves the bridge path
                # exclusively.
                if isinstance(event, Finish) and event.finish_reason == FinishReason.CANCELLED:
                    raise asyncio.CancelledError()
        except (asyncio.CancelledError, Exception):
            # Stream interrupted mid-flight (user /stop, pause, timeout,
            # error). The normal assistant-message append (llm node
            # ctx.history.append) never runs, so memory would lose this
            # partial content. Stash the live-streamed partial for the
            # agent's cancel/error handler to persist as an XML-marked
            # interrupted message, keeping memory aligned with the
            # transcript. Closing the event stream first forwards
            # GeneratorExit into the chain (the bridge cancels its
            # background chat_stream task).
            await aclose_llm_stream(events)
            if streamed_content or tool_names:
                state = get_react_state(ctx)
                if state is not None:
                    state.custom[TurnCustomKey.INTERRUPTED_PARTIAL] = {
                        "content": streamed_content,
                        "tool_names": tool_names,
                    }
            raise

        response = assembler.result()
        if streaming_emitter is not None:
            await streaming_emitter.emit_stream_end(resuming=bool(response.tool_calls))
        elif emitter is not None:
            # Non-streaming emitter: the folded response is delivered once at
            # end-of-call — the legacy plain-chat path's contract, preserved
            # verbatim so non-delta consumers (summarizer emitters, buffering
            # test emitters) keep seeing content/reasoning/stream_end.
            if response.content:
                await emitter.emit_content(response.content)
                await emitter.emit(ReActEvent.MODEL_OUTPUT, response.content)
            if response.reasoning_content:
                await emitter.emit(ReActEvent.MODEL_REASONING, response.reasoning_content)
            await emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response

    @staticmethod
    async def _drain_control(ctx: AgentContext) -> None:
        """Drain the control channel between every delta event so a
        CANCEL_TURN arriving mid-stream interrupts the LLM immediately
        (before the next event). Without this, cancel only takes effect
        after the full stream returns.
        """
        if ctx.runtime and ctx.runtime.control_channel:
            from modex_agent.hook.builtin.control_drain import drain_control_channel

            await drain_control_channel(
                ctx.runtime.control_channel,
                ctx,
                turn_uuid=ctx.runtime.turn_uuid,
            )
