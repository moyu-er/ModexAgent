"""S2: handle /cd, /pool <name>, /exit before persistence."""
from __future__ import annotations
import re
from bot.input_pipeline.context import BotInputContext
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult, Terminate

_POOL_RE = re.compile(r"^/([a-z][a-z0-9_-]*)$")

class EnvironmentControlStage(InputStage):
    def __init__(self, known_pools: set[str] | None = None) -> None:
        self._known_pools = known_pools or set()

    async def process(self, envelope: UserInputEnvelope, ctx: BotInputContext) -> StageResult:
        content = (envelope.content or "").strip()
        # /stop is owned by SessionControlStage (S3) — pass it through
        if content == "/stop":
            return Continue(value=envelope)
        # /pool <name> shortcut
        m = _POOL_RE.match(content)
        if m and (not self._known_pools or m.group(1) in self._known_pools):
            ctx.pool_session_store.set(envelope.conversation_id, m.group(1))
            return Terminate(reason="pool_switch", response={"message": f'switch to "{m.group(1)}" pool'})
        # /cd, /exit and other builtins via framework interception
        handled = await ctx.command_adapter._try_intercept_control(content, envelope.conversation_id)
        if handled:
            return Terminate(reason="environment_command")
        return Continue(value=envelope)
