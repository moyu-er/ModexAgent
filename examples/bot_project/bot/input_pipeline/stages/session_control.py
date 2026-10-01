"""S3: handle /stop (cancel current turn)."""
from __future__ import annotations

from bot.input_pipeline.context import BotInputContext
from modex_agent.pipeline.input.context import InputContext
from modex_agent.pipeline.input.envelope import UserInputEnvelope
from modex_agent.pipeline.input.stage import Continue, InputStage, StageResult, Terminate
from modex_agent.pipeline.input.stages.resolve_pool import resolve_session_routing


class SessionControlStage(InputStage):
    async def process(self, envelope: UserInputEnvelope, ctx: InputContext) -> StageResult:
        assert isinstance(ctx, BotInputContext), "Bot input stages require BotInputContext"
        if (envelope.content or "").strip() != "/stop":
            return Continue(value=envelope)
        full_sid = resolve_session_routing(envelope, ctx).full_session_id
        handled = await ctx.command_adapter._try_intercept_control("/stop", full_sid)
        if handled:
            return Terminate(reason="session_command")
        return Continue(value=envelope)
