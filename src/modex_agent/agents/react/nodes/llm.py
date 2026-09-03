"""LLMNode — assembles messages, calls LLM, writes assistant message."""

from __future__ import annotations

from modex_agent.agents.react.agent import ReActEvent

# ``constants.ReActEvent`` is the graph-runtime subset (9 events that route
# through ``ReactGraphRuntime.emit``). ``ITERATION_START`` / ``MODEL_OUTPUT``
# / ``MODEL_REASONING`` are NOT in it — they stay as direct
# ``ctx.agent_ctx.emitter.emit(...)`` calls (ADR-0033 D9.2: ``agent.ReActEvent``
# is a superset).
from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActHookPoint,
    ReActNode,
    ReActScope,
)
from modex_agent.agents.react.context import ReActGraphContext, get_agent_ctx
from modex_agent.agents.react.ids import next_call_id
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.media_injection import inject_multimodal
from modex_agent.agents.react.message_builder import build_assistant_message
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.runtime.dispatch import renew_dispatch_deadline
from modex_agent.runtime.enums import (
    MessageDeltaSource,
    OperationKind,
    TurnPhase,
)
from modex_agent.runtime.models import MessageDelta
from modex_graph import GraphPersistenceCoordinator
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node
from modex_graph.runtime import GraphRuntime


class LLMNode(Node[ReActTurnState]):
    def __init__(self, llm_client: ReactLlmClient, injection_drainer: InjectionDrainer) -> None:
        self.name = ReActNode.LLM
        self._llm_client = llm_client
        self._injection_drainer = injection_drainer

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state
        # The ReAct engine always passes a ``ReActGraphContext`` — reach the
        # wrapped ``AgentContext`` via ``user_data`` (typed ``Any`` on the
        # engine-side ``GraphContext`` ABC; narrowed here to ``AgentContext``).
        agent_ctx = get_agent_ctx(ctx)

        state.iteration += 1
        state.current_node = ReActNode.LLM
        state.phase = TurnPhase.RUNNING
        tm = agent_ctx.tool_manager
        if tm is not None:
            tools = tm.list_tools()
            tool_dict = {}
            for tool_name in tools:
                temp = tm.get_tool(tool_name)
                if temp is not None:
                    tool_dict[tool_name] = temp

        # Business-level max iterations check (ADR-0033 D9.3 layer 2). This
        # is the SOLE iteration cap on the ReAct path — the engine-level
        # ``compile(max_iterations=N)`` safety net is opt-in and ReAct does
        # not set it; exceeding this gate routes to AFTER via this static
        # edge as a controlled stop.
        if state.iteration > agent_ctx.max_iterations:
            await ctx.runtime.emit(GraphReActEvent.MAX_ITERATIONS, None, ctx)
            self.deliver(None, ReActNode.AFTER, ctx)
            return None

        agent_runtime = agent_ctx.runtime

        response: LLMResponse | None = None

        async def actual_iteration() -> None:
            nonlocal response
            # ITERATION_START is NOT in ``constants.ReActEvent`` (the
            # graph-runtime subset) — it stays as a direct emitter call.
            if agent_ctx.emitter is not None:
                await agent_ctx.emitter.emit(
                    ReActEvent.ITERATION_START,
                    {"iteration": state.iteration},
                )

            await ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx)

            await ctx.runtime.drain_control(ctx)

            if agent_runtime and agent_runtime.injection_queue:
                await self._injection_drainer.drain(agent_ctx)

            messages = await self._build_messages(
                agent_ctx,
                ctx.runtime,
                ctx.coordinator,
            )

            # Coerce to ChatMessage for a typed BEFORE_LLM hook payload (T10
            # prompt capture); ReactLlmClient.call coerces again internally.
            await ctx.runtime.dispatch_hook(
                ReActHookPoint.BEFORE_LLM,
                ctx,
                data={"request": [ChatMessage.coerce(m) for m in messages]},
            )

            # Declare the LLM no-progress budget into the dispatch deadline
            # before the call: while streaming, chunk callbacks keep renewing
            # (small amounts); a fully stalled stream expires per the budget.
            if agent_runtime is not None:
                renew_dispatch_deadline(
                    agent_runtime.safety.turn.dispatch_timeout_seconds
                )

            response = await self._llm_client.call(messages, agent_ctx)

            # Canonicalize tool-call ids BEFORE any consumer sees the
            # response: AFTER_LLM_RESPONSE hooks (ChatSpanHook), the
            # assistant history message, and ToolNode must all observe the
            # SAME id per call, or chat spans / tool spans / exported
            # trajectories cannot be joined by id. Providers that omit ids
            # get a Snowflake fallback (see agents.react.ids.next_call_id).
            if response.tool_calls:
                response = response.model_copy(
                    update={
                        "tool_calls": [
                            tc if tc.call_id else tc.model_copy(update={"call_id": next_call_id()})
                            for tc in response.tool_calls
                        ]
                    }
                )

            await ctx.runtime.dispatch_hook(
                ReActHookPoint.AFTER_LLM_RESPONSE,
                ctx,
                data={"response": response},
            )

            await ctx.runtime.drain_control(ctx)

            if response.finish_reason == FinishReason.ERROR.value:
                return

            # <think>-tag stripping is owned by the openai_compat engine
            # (parse_think_tags, ADR-0046); the node no longer re-sanitizes.
            content = response.content or ""

            assistant_msg = build_assistant_message(
                content,
                response.tool_calls,
                response.reasoning_content,
                reasoning_signature=response.reasoning_signature,
                reasoning_item_id=response.reasoning_item_id,
                reasoning_encrypted_content=response.reasoning_encrypted_content,
            )
            await agent_ctx.history.append(assistant_msg)
            state.add_operation(OperationKind.LLM_CALL, None)
            state.message_delta.append(
                MessageDelta(message=assistant_msg, source=MessageDeltaSource.ASSISTANT)
            )

        # ``ReactGraphRuntime.around`` handles the no-interceptor-chain case
        # as a pass-through (calls ``body()`` directly). When the chain exists
        # but has no ITERATION-scoped interceptors, ``around_iteration``
        # internally calls ``body()``. Both paths match the previous
        # ``if has_scope(ITERATION): around_iteration else: actual_iteration``.
        await ctx.runtime.around(ReActScope.ITERATION, ctx, actual_iteration)

        # AFTER_ITERATION fires at current-iteration-end (not next-iteration-start),
        # so all three exit paths (ERROR, TOOL, AFTER) get the dispatch (T16).
        await ctx.runtime.dispatch_hook(ReActHookPoint.AFTER_ITERATION, ctx)
        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            state.phase = TurnPhase.FAILED
            error_text = response.error or response.content or "LLM request failed"
            self.deliver({"error": error_text}, ReActNode.AFTER, ctx)
            return None

        if response is not None and response.tool_calls:
            self.deliver(None, ReActNode.TOOL, ctx)
            return None

        await ctx.runtime.emit(
            GraphReActEvent.ITERATION_END,
            {"iteration": state.iteration, "has_tool_calls": False},
            ctx,
        )
        self.deliver(None, ReActNode.AFTER, ctx)
        return None

    async def _build_messages(
        self,
        ctx: AgentContext,
        graph_runtime: GraphRuntime,
        coordinator: GraphPersistenceCoordinator,
    ) -> list[ChatMessage]:
        messages: list[dict[str, object]] = []

        system_content = await ctx.get_resolved_system_prompt()
        if system_content:
            messages.append({"role": "system", "content": system_content})

        messages.extend(await ctx.to_messages())

        from modex_agent.agents.react.state import get_react_state

        state = get_react_state(ctx)
        if state is not None:
            graph_ctx = ReActGraphContext(
                state=state,
                runtime=graph_runtime,
                user_data=ctx,
                coordinator=coordinator,
            )
            messages = await graph_runtime.apply_governance(messages, graph_ctx)

        typed = [ChatMessage.coerce(m) for m in messages]
        return inject_multimodal(typed, ctx)


__all__ = ["LLMNode"]
