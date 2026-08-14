"""Resolve the active workspace for the message and stamp it on the envelope.

Resolution priority:
1. ``envelope.metadata[RoutingMeta.WORKSPACE]`` if set (WebUI will populate this from the
   wire payload in Task 5; absent for IM and for legacy WebUI -> fall through).
2. ``ctx.current_ws()`` (the injected provider; default home / ``Path.cwd()``
   in Task 1; Task 4 will inject the real IM adapter provider).

The resolved workspace is written as ``str(Path)`` into
``envelope.metadata[RoutingMeta.WORKSPACE]`` to match the other routing
metadata values (all strings). ``EnqueueStage`` converts it back to ``Path``
for ``InputMessage.workspace``.
"""

from __future__ import annotations

from pathlib import Path

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult


class ResolveWorkspaceStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        explicit = envelope.metadata.get(RoutingMeta.WORKSPACE)
        resolved = Path(str(explicit)) if explicit is not None else ctx.current_ws()
        envelope.metadata[RoutingMeta.WORKSPACE] = str(resolved)
        return Continue(value=envelope)
