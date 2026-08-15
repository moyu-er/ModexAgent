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
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
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
        state.current_node = ReActNode.AFTER

        # Deliver-ized: LLM infrastructure errors arrive as a deliver payload
        # {"error": text} from LLMNode. Tool execution failures set phase=FAILED
        # in ToolNode. Both converge here into the FAILED branch (02 ticket:
        # "AfterTurnNode ERROR branch merges into FAILED").
        error_text: str | None = None
        for payload in integrated_input.payloads:
            content = payload.content
            if isinstance(content, dict) and "error" in content:
                error_text = str(content["error"])
                if state.phase != TurnPhase.CANCELLED:
                    state.phase = TurnPhase.FAILED
                break

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
                error=error_text or "tool execution error",
                stop_reason=StopReason.ERROR,
                messages=messages,
                attachments=agent_ctx.attachments,
            )
        else:
            last_assistant: ChatMessage | None = None
            for md in reversed(state.message_delta):
                if md.message.role == MessageRole.ASSISTANT:
                    last_assistant = md.message
                    break

            if last_assistant is not None and not last_assistant.tool_calls:
                raw_content = last_assistant.content
                result_content = raw_content if isinstance(raw_content, str) else ""
                reasoning: str | None = getattr(last_assistant, "reasoning_content", None)
                result = AgentResult(
                    content=result_content,
                    reasoning=reasoning,
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
            self.deliver(None, ReActNode.BEFORE, ctx)
        else:
            self.deliver(None, ReActNode.END, ctx)
        return None


__all__ = ["AfterTurnNode"]
