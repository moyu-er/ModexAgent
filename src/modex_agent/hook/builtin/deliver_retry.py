"""DeliverRetryHook requests continuation when an agent stops without delivering."""

from __future__ import annotations

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason
from modex_agent.core.types import LLMResponse
from modex_agent.hook.abc import AfterLLMResponseHook
from modex_agent.runtime.enums import TurnCustomKey


class DeliverRetryHook(AfterLLMResponseHook):
    """Request graph-internal continuation when the agent omits delivery.

    Uses AFTER_LLM_RESPONSE (not AFTER_TURN) because AFTER_TURN fires in
    actual_turn() after the graph ends — too late for graph-internal
    continuation. AFTER_LLM_RESPONSE fires in LLMNode when LLM returns stop,
    before AfterTurnNode runs. AfterTurnNode owns reminder injection so the
    reminder follows the assistant response in history.
    """

    @property
    def name(self) -> str:
        return "deliver_retry"

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        if ctx.graph_context is None:
            return
        if response.finish_reason != FinishReason.STOP:
            return
        if response.tool_calls:
            return

        react_state = get_react_state(ctx)
        if react_state is None:
            return

        deliver_count = react_state.custom.get(TurnCustomKey.GRAPH_DELIVER_COUNT, 0)
        if deliver_count > 0:
            return

        max_turns = react_state.custom.get(TurnCustomKey.MAX_TURNS, 1)
        if react_state.turn_attempt >= max_turns:
            return

        react_state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
