"""LLMNode — assembles messages, calls LLM, writes assistant message."""

from __future__ import annotations

from modex_agent.agents.react.agent import ReActEvent

# ``constants.ReActEvent`` is the graph-runtime subset (9 events that route
# through ``ReactGraphRuntime.emit``). ``ITERATION_START`` / ``MODEL_OUTPUT``
# / ``MODEL_REASONING`` are NOT in it — they stay as direct
# ``ctx.emitter.emit(...)`` calls (ADR-0033 D9.2: ``agent.ReActEvent`` is a
# superset).
from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent
from modex_agent.agents.react.constants import (
    ReActHookPoint,
    ReActNode,
    ReActReason,
    ReActScope,
)
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.message_builder import build_assistant_message
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState, get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.graph.node import Node, NodeTransition
from modex_agent.core.types import MessageRole
from modex_agent.ioc.configs.llm import Modality
from modex_agent.media.media_utils import build_inline_image_block
from modex_agent.runtime.dispatch import current_dispatch_deadline
from modex_agent.runtime.enums import (
    MessageDeltaSource,
    OperationKind,
    TurnCustomKey,
    TurnPhase,
)
from modex_agent.runtime.models import MessageDelta
from modex_graph.context import GraphContext


def _renew_dispatch_deadline() -> None:
    deadline = current_dispatch_deadline.get()
    if deadline is not None:
        deadline.renew()


def enrich_inline_attachments(
    messages: list[dict[str, object]],
    ctx: AgentContext,
) -> list[dict[str, object]]:
    """Inline current-turn image attachments into the current turn's user message.

    Mechanism A activation (ADR-0014); ADR-0013 §10 paved the capability seam.
    Runs AFTER governance so governance and persisted history only ever see the
    text-reference form. Returns a NEW messages list (copies the one mutated
    message) for the transient LLM call; persisted history is never touched.

    Bails out unchanged when: no runtime, IMAGE capability absent, no react
    state, no/empty attachments, or no user message at the tail position.
    """
    runtime = ctx.runtime
    if (
        runtime is None
        or not runtime.model_capabilities
        or not runtime.model_capabilities.supports(Modality.IMAGE)
    ):
        return messages

    state = get_react_state(ctx)
    attachments = state.custom.get(TurnCustomKey.INLINE_ATTACHMENTS) if state else None
    if not attachments:
        return messages

    # Find the last user-role message (governance may have appended system
    # messages after it, so a simple messages[-1] is not safe). The agent→user
    # normalization happens upstream in to_messages, so "user" is correct.
    user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == str(MessageRole.USER):
            user_idx = i
            break
    if user_idx < 0:
        return messages

    cache = state.custom.setdefault(TurnCustomKey.INLINE_IMAGE_CACHE, {})
    tail: list[dict[str, object]] = []
    for att in attachments:
        # Base64 is encoded once per attachment id; later ReAct iterations
        # reuse the cached blocks (ADR-0014 §5).
        blocks = cache.get(att.id)
        if blocks is None:
            blocks = build_inline_image_block(att)
            cache[att.id] = blocks
        tail.extend(blocks)

    if not tail:
        return messages

    target = messages[user_idx]
    text = target.get("content") or ""
    new_content: list[dict[str, object]] = [{"type": "text", "text": text}, *tail]
    enriched = {**target, "content": new_content}
    return [*messages[:user_idx], enriched, *messages[user_idx + 1 :]]


class LLMNode(Node):
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
            await self._emit(ctx, state, GraphReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        runtime = ctx.runtime
        graph_ctx = self._graph_ctx(state, ctx)

        async def actual_iteration():
            # ITERATION_START is NOT in ``constants.ReActEvent`` (the
            # graph-runtime subset) — it stays as a direct emitter call.
            if ctx.emitter is not None:
                await ctx.emitter.emit(
                    ReActEvent.ITERATION_START,
                    {"iteration": state.iteration},
                )

            # Signal completion of the previous iteration (skip on first iter).
            # Per ADR-0033 D5 rule 1: iteration hooks are node-controlled
            # (NOT engine-auto-invoked). Dispatch at the same code points as
            # before migration — timing is preserved by construction.
            if state.iteration > 1:
                await graph_ctx.runtime.dispatch_hook(ReActHookPoint.AFTER_ITERATION, graph_ctx)

            await graph_ctx.runtime.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, graph_ctx)

            await graph_ctx.runtime.drain_control(graph_ctx)

            if runtime and runtime.injection_queue:
                await self._injection_drainer.drain(ctx)

            messages = await self._build_messages(ctx)
            response = await self._llm_client.call(messages, ctx)

            await graph_ctx.runtime.dispatch_hook(
                ReActHookPoint.AFTER_LLM_RESPONSE,
                graph_ctx,
                data={"response": response},
            )

            await graph_ctx.runtime.drain_control(graph_ctx)

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

        # ``ReactGraphRuntime.around`` handles the no-interceptor-chain case
        # as a pass-through (calls ``body()`` directly). When the chain exists
        # but has no ITERATION-scoped interceptors, ``around_iteration``
        # internally calls ``body()``. Both paths match the previous
        # ``if has_scope(ITERATION): around_iteration else: actual_iteration``.
        await graph_ctx.runtime.around(ReActScope.ITERATION, graph_ctx, actual_iteration)

        _renew_dispatch_deadline()

        response = state.llm_response
        if response is not None and response.finish_reason == FinishReason.ERROR.value:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        if response is not None and response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)

        await self._emit(
            ctx,
            state,
            GraphReActEvent.ITERATION_END,
            {"iteration": state.iteration, "has_tool_calls": False},
        )
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)

    @staticmethod
    def _graph_ctx(
        state: ReActTurnState,
        ctx: AgentContext,
    ) -> GraphContext[ReActTurnState]:
        """Build a thin ``GraphContext`` wrapper for ``ReactGraphRuntime`` calls.

        ReAct still uses the old ``core/graph/`` engine, which passes
        ``AgentContext`` to ``Node.execute``. ``ReactGraphRuntime`` methods
        take ``GraphContext`` — this wrapper bridges the gap by stuffing
        the ``AgentContext`` into ``user_data`` and the typed
        ``ReActTurnState`` into ``state``. Ticket 05 will make this wrapper
        unnecessary when the new engine passes ``ReActGraphContext``
        directly.
        """
        runtime = ctx.runtime
        assert runtime is not None  # get_react_state(ctx) early-returned otherwise
        graph_runtime = runtime.graph_runtime or ReactGraphRuntime()
        return GraphContext(state=state, runtime=graph_runtime, user_data=ctx)

    @staticmethod
    async def _emit(
        ctx: AgentContext,
        state: ReActTurnState,
        event: GraphReActEvent,
        data: object = None,
    ) -> None:
        """Route an emit call through ``ReactGraphRuntime.emit``.

        ``ReactGraphRuntime.emit`` maps the str event to ``agent.ReActEvent``
        and calls ``emitter.emit``. No-op when no emitter is wired.
        """
        runtime = ctx.runtime
        if runtime is None:
            return
        graph_runtime = runtime.graph_runtime or ReactGraphRuntime()
        graph_ctx = GraphContext(state=state, runtime=graph_runtime, user_data=ctx)
        await graph_runtime.emit(event, data, graph_ctx)

    async def _build_messages(
        self,
        ctx: AgentContext,
    ) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []

        # Use pipeline for dynamic system prompt if available
        if ctx.system_prompt_pipeline is not None:
            system_content = await ctx.system_prompt_pipeline.get_or_refresh()
            if system_content:
                messages.append({"role": "system", "content": system_content})
        elif ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})

        messages.extend(await ctx.to_messages())

        # Route governance through ``ReactGraphRuntime.apply_governance`` when
        # a runtime is wired; otherwise skip (matches the original
        # ``governance = ctx.runtime.governance if ctx.runtime else None``
        # graceful-None path).
        runtime = ctx.runtime
        if runtime is not None:
            state = get_react_state(ctx)
            if state is not None:
                graph_runtime = runtime.graph_runtime or ReactGraphRuntime()
                graph_ctx = GraphContext(state=state, runtime=graph_runtime, user_data=ctx)
                messages = await graph_runtime.apply_governance(messages, graph_ctx)

        return enrich_inline_attachments(messages, ctx)
