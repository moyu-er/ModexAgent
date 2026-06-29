"""LLMNode — assembles messages, calls LLM, writes assistant message."""

from __future__ import annotations

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ReActNode, ReActReason
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.message_builder import build_assistant_message
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.graph.node import Node, NodeTransition
from modex_agent.hook import HookPayload, HookPoint
from modex_agent.interceptor.abc import InterceptorScope, IterationContext
from modex_agent.ioc.configs.llm import Modality
from modex_agent.media.models import Attachment
from modex_agent.runtime.dispatch import current_dispatch_deadline
from modex_agent.runtime.enums import (
    MessageDeltaSource,
    OperationKind,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import MessageDelta
from modex_agent.utils.media_utils import build_inline_image_block


def _renew_dispatch_deadline() -> None:
    deadline = current_dispatch_deadline.get()
    if deadline is not None:
        deadline.renew()


def _attachment_blocks(att: Attachment, cache: dict[str, list[dict[str, object]]]) -> list[dict[str, object]]:
    """Return the caption + image_url block pair for ``att``, cached per turn.

    Base64 is encoded once per attachment id; later ReAct iterations reuse the
    cached list (ADR-0014 §5). Mutates ``cache`` in place.
    """
    cached = cache.get(att.id)
    if cached is not None:
        return cached
    blocks = build_inline_image_block(att)
    cache[att.id] = blocks
    return blocks


def enrich_inline_attachments(
    messages: list[dict[str, object]],
    ctx: AgentContext,
) -> list[dict[str, object]]:
    """Inline current-turn image attachments into the LAST user-role message.

    Mechanism A activation (ADR-0014); ADR-0013 §10 paved the capability seam.
    Runs AFTER governance so governance and persisted history only ever see the
    text-reference form. The persisted history is never touched — this returns a
    NEW messages list (copies the one mutated message) for the transient LLM
    call. Bails out unchanged when any gate fails: no runtime, no IMAGE
    capability, no react state, no/empty attachments, or no user message.
    """
    runtime = ctx.runtime
    if runtime is None:
        return messages
    caps = runtime.model_capabilities
    if caps is None or not caps.supports(Modality.IMAGE):
        return messages

    state = get_react_state(ctx)
    if state is None:
        return messages
    attachments = state.custom.get(TurnCustomKey.INLINE_ATTACHMENTS)
    if not attachments:
        return messages

    # Locate the LAST user-role message (agent→user normalization happened
    # upstream in to_messages, so a plain "user" check is correct).
    user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            user_idx = i
            break
    if user_idx < 0:
        return messages

    cache = state.custom.setdefault(TurnCustomKey.INLINE_IMAGE_CACHE, {})
    tail: list[dict[str, object]] = []
    for att in attachments:
        tail.extend(_attachment_blocks(att, cache))

    if not tail:
        return messages

    target = messages[user_idx]
    existing = target.get("content")
    if isinstance(existing, list):
        new_content = [*existing, *tail]
    else:
        text = existing if existing is not None else ""
        new_content = [{"type": "text", "text": text}, *tail]

    enriched = {**target, "content": new_content}
    return [*messages[:user_idx], enriched, *messages[user_idx + 1 :]]


class LLMNode(Node):
    """Calls LLM, writes assistant message, routes to ToolNode or EndNode."""

    def __init__(self, llm_client: ReactLlmClient, injection_drainer: InjectionDrainer) -> None:
        super().__init__(ReActNode.LLM)
        self._llm_client = llm_client
        self._injection_drainer = injection_drainer

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        state = get_react_state(ctx)
        if state is None:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        state.iteration += 1
        state.current_node = ReActNode.LLM
        state.phase = TurnPhase.RUNNING
        tm = ctx.tool_manager
        if tm is not None:
            tools = tm.list_tools()
            tool_dict = dict()
            for tool_name in tools:
                temp = tm.get_tool(tool_name)
                if temp is not None:
                    tool_dict[tool_name] = temp

        if state.iteration > ctx.max_iterations:
            if ctx.emitter is not None:
                await ctx.emitter.emit(ReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        runtime = ctx.runtime

        async def actual_iteration():
            if ctx.emitter is not None:
                await ctx.emitter.emit(
                    ReActEvent.ITERATION_START,
                    {"iteration": state.iteration},
                )

            if runtime and runtime.hooks:
                await runtime.hooks.dispatch(HookPoint.BEFORE_ITERATION, ctx)

            # Drain control commands before LLM call
            if ctx.runtime and ctx.runtime.control_channel:
                from modex_agent.hook.builtin.control_drain import drain_control_channel

                await drain_control_channel(
                    ctx.runtime.control_channel,
                    ctx,
                    turn_uuid=ctx.runtime.turn_uuid,
                )

            if runtime and runtime.injection_queue:
                await self._injection_drainer.drain(ctx)

            messages = await self._build_messages(ctx)
            response = await self._llm_client.call(messages, ctx)

            if runtime and runtime.hooks:
                await runtime.hooks.dispatch(
                    HookPoint.AFTER_LLM_RESPONSE,
                    ctx,
                    payload=HookPayload(data={"response": response}),
                )

            # Drain control commands after LLM response
            if ctx.runtime and ctx.runtime.control_channel:
                from modex_agent.hook.builtin.control_drain import drain_control_channel

                await drain_control_channel(
                    ctx.runtime.control_channel,
                    ctx,
                    turn_uuid=ctx.runtime.turn_uuid,
                )

            if response.finish_reason == FinishReason.ERROR.value:
                state.llm_response = response
                return

            from modex_agent.utils.helpers import strip_think

            # If the provider did NOT separate reasoning_content (non-standard API),
            # sanitize possible <think> tags embedded in content.
            content = response.content or ""
            if response.reasoning_content is None:
                content = strip_think(content) or ""

            assistant_msg = build_assistant_message(
                content,
                response.tool_calls,
                response.reasoning_content,
            )
            await ctx.history.append(assistant_msg)
            state.llm_response = response
            state.add_operation(OperationKind.LLM_CALL, None)
            state.message_delta.append(
                MessageDelta(message=assistant_msg, source=MessageDeltaSource.ASSISTANT)
            )

        if (
            runtime
            and runtime.interceptors
            and runtime.interceptors.has_scope(InterceptorScope.ITERATION)
        ):
            await runtime.interceptors.around_iteration(
                ctx,
                IterationContext(iteration=state.iteration, turn_id=str(ctx.session)),
                actual_iteration,
            )
        else:
            await actual_iteration()

        _renew_dispatch_deadline()

        response = state.llm_response
        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        if response is not None and response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)

        if ctx.emitter is not None:
            await ctx.emitter.emit(
                ReActEvent.ITERATION_END,
                {
                    "iteration": state.iteration,
                    "has_tool_calls": False,
                },
            )
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)

    async def _build_messages(self, ctx: AgentContext) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []

        # Use pipeline for dynamic system prompt if available
        if ctx.system_prompt_pipeline is not None:
            system_content = await ctx.system_prompt_pipeline.get_or_refresh()
            if system_content:
                messages.append({"role": "system", "content": system_content})
        elif ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})

        messages.extend(await ctx.to_messages())

        governance = ctx.runtime.governance if ctx.runtime else None
        if governance is not None:
            messages = await governance.apply(messages)
        return enrich_inline_attachments(messages, ctx)
