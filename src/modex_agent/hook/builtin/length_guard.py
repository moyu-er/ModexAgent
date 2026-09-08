"""LengthGuardHook — recover degenerate turn endings, fail honestly on exhaustion.

A degenerate ending is a turn attempt whose last LLM call produced nothing
usable: ``finish_reason=length`` with empty content and zero tool calls (all
budget burned in reasoning), ``finish_reason=stop`` with empty content and
zero tool calls, or ``finish_reason=length`` with non-empty content (prose
truncated at the max_tokens ceiling). Before this hook, the first case was
marked ``stop_reason=COMPLETED`` — a silent failure (v5 harness gap).

Recovery: inject a no-thinking nudge ``<system-reminder>`` and set both
continuation flags so the ``AfterTurnNode`` gate re-enters the ReAct loop
(``CONTINUATION_RENEW_MAX_TURNS`` is required — without it the gate drops the
request at the MAX_TURNS ceiling).

Honest failure: after ``MAX_NUDGES`` consecutive degenerate endings with no
productive response in between, the hook mutates the turn's ``AgentResult``
in place to ``StopReason.ERROR`` (no new StopReason enum) instead of letting
a content-less turn complete silently. ``AgentResult`` is a non-frozen
BaseModel and ``AfterTurnNode`` dispatches ``AFTER_TURN`` with the same
object it wrote to ``state.result`` — the in-place mutation is what
``EndNode`` reads.
"""

from __future__ import annotations

from typing import Final

from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import MessageRole
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.hook.abc import AfterLLMResponseHook, AfterTurnHook
from modex_agent.runtime.enums import TurnCustomKey

MAX_NUDGES: Final = 10

NUDGE_NO_OUTPUT: Final = (
    "Your previous response ended with no visible content and no tool calls. "
    "Do NOT think or reason further. In one sentence state the next concrete "
    "step, then immediately call the tool that executes it."
)
NUDGE_TRUNCATED: Final = (
    "Your previous response was cut off at the max_tokens limit. Do not "
    "re-think or rewrite it. Continue exactly where the output stopped, or "
    "immediately call the tool that completes the work."
)


class LengthGuardHook(AfterLLMResponseHook, AfterTurnHook):
    """Recover turns that end degenerately at the max_tokens ceiling (or empty),
    and fail honestly when nudging no longer produces progress."""

    @property
    def name(self) -> str:
        return "length_guard"

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        state = get_react_state(ctx)
        if state is None:
            return
        state.custom[TurnCustomKey.LAST_LLM_FINISH_REASON] = response.finish_reason
        if (response.content and response.content.strip()) or response.tool_calls:
            state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] = 0

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason in (StopReason.TURN_CANCELLED, StopReason.ERROR):
            return
        state = get_react_state(ctx)
        if state is None:
            return
        finish = state.custom.get(TurnCustomKey.LAST_LLM_FINISH_REASON)
        if finish not in (FinishReason.LENGTH, FinishReason.STOP):
            return
        if result.stop_reason != StopReason.COMPLETED:
            return

        empty = not (result.content or "").strip()
        if finish == FinishReason.STOP and not empty:
            return  # normal completion

        nudges = int(state.custom.get(TurnCustomKey.LENGTH_GUARD_NUDGES, 0)) + 1
        state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] = nudges

        if nudges > MAX_NUDGES:
            result.stop_reason = StopReason.ERROR
            result.error = (
                f"length-guard: exhausted {MAX_NUDGES} nudges after degenerate "
                "max_tokens/empty endings with no progress"
            )
            return

        text = NUDGE_NO_OUTPUT if empty else NUDGE_TRUNCATED
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(text),
            }
        )
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] = True
