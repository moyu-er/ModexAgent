"""S2: handle /cd, /pool <name>, /exit before persistence.

IM-only stage — the WebUI pipeline (build_webui_pipeline) does NOT include
S2/S3.  Control commands (/pwd, /cd, /exit) are routed through
ctx.command_adapter._try_intercept_control(), which is configured via
configure_control_filter() in BotService._initialize_pool()."""
from __future__ import annotations
import re
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import conversation_session_prefix
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
        # The prefix is the agent-independent conversation identity and the
        # sole key for the pool store (resolved before the agent is known).
        session_prefix = conversation_session_prefix(envelope, ctx)
        # /pool <name> shortcut
        m = _POOL_RE.match(content)
        if m and (not self._known_pools or m.group(1) in self._known_pools):
            ctx.pool_session_store.set(session_prefix, m.group(1))
            return Terminate(reason="pool_switch", response={"message": f'switch to "{m.group(1)}" pool'})
        # /cd, /exit and other builtins via framework interception.
        # Target the full session id of the CURRENT pool so control commands
        # (e.g. CANCEL_TURN) hit the right session.
        current_pool = ctx.pool_session_store.get(session_prefix, ctx.default_pool)
        agent = ctx.agent_for_pool(current_pool)
        full_sid = f"{session_prefix}.{agent}"
        handled = await ctx.command_adapter._try_intercept_control(content, full_sid)
        if handled:
            return Terminate(reason="environment_command")
        return Continue(value=envelope)
