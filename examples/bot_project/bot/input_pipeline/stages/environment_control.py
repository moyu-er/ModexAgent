"""S2: handle /cd, /pool <name>, /exit, /pwd before persistence.

IM-only stage — the WebUI pipeline (build_webui_pipeline) does NOT include
S2/S3.  Control commands are handled directly here:

- /cd <dir>  -> validates via WorkspaceController, sets adapter.current_ws,
                persists via adapter.save_current_ws(), terminates with notice.
- /exit      -> resets adapter.current_ws to adapter.home, persists, terminates.
- /pwd       -> terminates with current workspace notice.
- /pool <n>  -> records pool choice and terminates.
- /stop      -> passes through to S3 (SessionControlStage).
- Other commands -> delegated to ctx.command_adapter._try_intercept_control().

/continue is NOT handled here — it lives in the shared CommandDispatchStage
(used by both IM and WebUI pipelines) so cross-channel commands have one home.
"""
from __future__ import annotations

import re

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import conversation_session_prefix
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult, Terminate
from modex_agent.workspace.control import WorkspaceController

_POOL_RE = re.compile(r"^/([a-z][a-z0-9_-]*)$")


class EnvironmentControlStage(InputStage):
    def __init__(
        self,
        known_pools: set[str] | None = None,
        *,
        workspace_controller: WorkspaceController | None = None,
    ) -> None:
        self._known_pools = known_pools or set()
        self._workspace_controller = workspace_controller

    async def process(self, envelope: UserInputEnvelope, ctx: BotInputContext) -> StageResult:
        content = (envelope.content or "").strip()
        session_prefix = conversation_session_prefix(envelope, ctx)

        # /stop is owned by SessionControlStage (S3) — pass it through
        if content == "/stop":
            return Continue(value=envelope)

        # /cd <dir> — must be checked BEFORE /pool regex because /cd is not a pool name
        if content.startswith("/cd "):
            target = content[4:].strip()
            return await self._handle_cd(ctx, target)

        # /exit
        if content == "/exit":
            return await self._handle_exit(ctx)

        # /pwd
        if content == "/pwd":
            return await self._handle_pwd(ctx)

        # /pool <name> shortcut
        m = _POOL_RE.match(content)
        if m and (not self._known_pools or m.group(1) in self._known_pools):
            ctx.pool_session_store.set(session_prefix, m.group(1))
            return Terminate(
                reason="pool_switch", response={"message": f'switch to "{m.group(1)}" pool'}
            )

        # Other commands delegated to adapter control interception
        current_pool = ctx.pool_session_store.get(session_prefix, ctx.default_pool)
        agent = ctx.agent_for_pool(current_pool)
        full_sid = f"{session_prefix}.{agent}"
        handled = await ctx.command_adapter._try_intercept_control(content, full_sid)
        if handled:
            return self._terminate_with()
        return Continue(value=envelope)

    async def _handle_cd(self, ctx: BotInputContext, target: str) -> StageResult:
        """Handle /cd <dir>: validate via controller, set adapter.current_ws, persist, terminate."""
        adapter = ctx.command_adapter
        controller = self._workspace_controller

        if controller is None:
            return self._terminate_with("cd: workspace controller not available")

        result = await controller.open_workspace(target)
        if not result.success:
            return self._terminate_with(result.notice)

        adapter.current_ws = result.current_path
        adapter.save_current_ws()

        return self._terminate_with(result.notice)

    async def _handle_exit(self, ctx: BotInputContext) -> StageResult:
        """Handle /exit: reset current_ws to home, persist, terminate."""
        adapter = ctx.command_adapter
        adapter.current_ws = adapter.home
        adapter.save_current_ws()

        return self._terminate_with(f"exit: returned to {adapter.home}")

    async def _handle_pwd(self, ctx: BotInputContext) -> StageResult:
        """Handle /pwd: return current workspace notice."""
        current = ctx.current_ws()
        return self._terminate_with(f"pwd: {current}")

    def _terminate_with(self, message: str | None = None) -> StageResult:
        """Return a Terminate result with an optional notice message."""
        if message is None:
            return Terminate(reason="environment_command")
        return Terminate(reason="environment_command", response={"message": message})
