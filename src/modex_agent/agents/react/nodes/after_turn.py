"""AfterTurnNode -- brackets the LLM<->TOOL loop at the end of a turn attempt.

Constructs the preliminary ``AgentResult`` from turn state, writes
``state.result``, then routes to ``BEFORE`` (continuation) or ``END``
(terminal).

The ``AgentResult`` construction logic was moved here from
``EndNode``. ``EndNode`` now reads ``state.result`` (constructed by this
node) and handles terminal events only. ``AfterTurnNode`` only constructs
the result and decides the next hop; ``EndNode`` reads ``state.result``
when the turn is truly terminal (ADR-0033 D9.3, rule 15 convergence -- one
result construction path).
"""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.context import get_agent_ctx
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.runtime.enums import TurnCustomKey, TurnPhase
from modex_graph.context import GraphContext
from modex_graph.integration import IntegratedInput
from modex_graph.node import Node


class AfterTurnNode(Node[ReActTurnState]):
    """Turn-attempt lifecycle node: constructs result, writes state.result, routes continuation or terminal."""

    def __init__(self) -> None:
        self.name = ReActNode.AFTER

    async def execute(
        self,
        ctx: GraphContext[ReActTurnState],
        integrated_input: IntegratedInput,
    ) -> None:
        state = ctx.state
        agent_ctx = get_agent_ctx(ctx)
        response = state.llm_response
        state.current_node = ReActNode.AFTER

        messages = [md.message for md in state.message_delta]

        if state.phase == TurnPhase.CANCELLED:
            result = AgentResult(
                content="turn cancelled",
                stop_reason=StopReason.TURN_CANCELLED,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
        elif state.phase == TurnPhase.FAILED:
            result = AgentResult(
                error="tool execution error",
                stop_reason=StopReason.ERROR,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
        elif response is not None and response.finish_reason == FinishReason.ERROR.value:
            error_text = response.error or response.content or "LLM request failed"
            result = AgentResult(
                error=error_text,
                stop_reason=StopReason.ERROR,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
        elif response is not None and not response.tool_calls:
            result = AgentResult(
                content=response.content or "",
                reasoning=response.reasoning_content,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
        else:
            result = AgentResult(
                content="max iterations reached",
                stop_reason=StopReason.MAX_ITERATIONS,
                messages=messages,
                attachments=agent_ctx.attachments,
            )

        # ADR-0033 D9.3: write the typed ``state.result`` field. The caller's
        # ``run()`` and ``EndNode`` read it after the engine returns.
        state.result = result

        await ctx.runtime.dispatch_hook(ReActHookPoint.AFTER_TURN, ctx, {"result": result})

        # Continuation gate: one-shot flags, consumed regardless of path.
        # CONTINUATION_REQUEST — any AfterTurnHook wants another turn attempt.
        # CONTINUATION_RENEW_MAX_TURNS — a hook authorizes extending MAX_TURNS
        #   past the current upper bound (watchdog renewal).  Only one hook
        #   (TodoContinuationHook) sets this today.  Gate increments
        #   MAX_TURNS by 1 only once regardless of how many hooks set it.
        max_turns = state.custom.get(TurnCustomKey.MAX_TURNS, 3)
        has_request = TurnCustomKey.CONTINUATION_REQUEST in state.custom
        has_renew = TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS in state.custom

        state.custom.pop(TurnCustomKey.CONTINUATION_REQUEST, None)
        state.custom.pop(TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS, None)

        if (
            has_request
            and state.phase != TurnPhase.CANCELLED
            and (state.turn_attempt < max_turns or has_renew)
        ):
            if state.turn_attempt >= max_turns:
                state.custom[TurnCustomKey.MAX_TURNS] = max_turns + 1
            self.deliver(result, ReActNode.BEFORE, ctx)
        else:
            self.deliver(result, ReActNode.END, ctx)
        return None


__all__ = ["AfterTurnNode"]
