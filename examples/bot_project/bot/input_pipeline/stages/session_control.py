"""S3: handle /stop (cancel current turn)."""
from __future__ import annotations

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import resolve_session_routing
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult, Terminate


class SessionControlStage(InputStage):
    async def process(self, envelope: UserInputEnvelope, ctx: BotInputContext) -> StageResult:
        if (envelope.content or "").strip() != "/stop":
            return Continue(value=envelope)
        _, _, full_sid = resolve_session_routing(envelope, ctx)
        handled = await ctx.command_adapter._try_intercept_control("/stop", full_sid)
        if handled:
            return Terminate(reason="session_command")
        return Continue(value=envelope)
