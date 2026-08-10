"""Current-time injection at fresh ReAct turn start."""

from __future__ import annotations

from datetime import datetime

from modex_agent.core.agent import AgentContext
from modex_agent.core.message_utils import wrap_system_reminder
from modex_agent.core.types import MessageRole
from modex_agent.hook.abc import StartNodeTurnHook
from modex_agent.utils.timezone import get_user_timezone


class CurrentTimeInjectionHook(StartNodeTurnHook):
    """Inject second-precision current time at fresh-turn start."""

    @property
    def name(self) -> str:
        return "current_time_injection"

    async def start_node_turn(self, ctx: AgentContext) -> None:
        timezone = get_user_timezone()
        current_time = datetime.now(timezone)
        timezone_name = getattr(timezone, "key", None) or str(timezone)
        reminder = (
            f"Current time: {current_time:%Y-%m-%d %H:%M:%S} "
            f"({timezone_name}, {current_time:%A})"
        )
        await ctx.history.append(
            {
                "role": str(MessageRole.SYSTEM_REMINDER),
                "content": wrap_system_reminder(reminder),
            }
        )
