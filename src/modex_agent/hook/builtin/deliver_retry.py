"""DeliverRetryHook requests continuation when an agent stops without delivering."""

from __future__ import annotations

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.core.types import MessageRole
from modex_agent.hook.abc import AfterTurnHook
from modex_agent.runtime.enums import TurnCustomKey


class DeliverRetryHook(AfterTurnHook):
    """Request graph-internal continuation when the agent omits delivery.

    Uses AFTER_TURN (turn-attempt level, dispatched in AfterTurnNode before
    continuation routing), which is the correct timing for setting
    CONTINUATION_REQUEST because it covers both normal stop and max-iteration
    exits. The old AFTER_LLM_RESPONSE timing missed max-iteration exits.
    """

    @property
    def name(self) -> str:
        return "deliver_retry"

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason in (StopReason.TURN_CANCELLED, StopReason.ERROR):
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

        if TurnCustomKey.CONTINUATION_REQUEST in react_state.custom:
            return

        react_state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        reminder = (
            "You ended without calling the `deliver` tool. Your regular "
            "text output was NOT delivered to anyone. You MUST call "
            "`deliver` with your complete work output and an explicit "
            "target. Do not finish without delivering."
        )
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(reminder),
            }
        )
