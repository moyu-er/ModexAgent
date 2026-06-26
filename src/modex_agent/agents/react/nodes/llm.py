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
from modex_agent.runtime.dispatch import current_dispatch_deadline
from modex_agent.runtime.enums import MessageDeltaSource, OperationKind, TurnPhase
from modex_agent.runtime.models import MessageDelta


def _renew_dispatch_deadline() -> None:
    deadline = current_dispatch_deadline.get()
    if deadline is not None:
        deadline.renew()


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
        return messages
