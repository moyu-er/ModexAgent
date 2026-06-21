from __future__ import annotations

from pathlib import Path
from typing import Callable
from unittest.mock import MagicMock

from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import ParsedSkill, SkillRegistry


class _NoSkillRegistry(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None


def attach_default_pipeline(
    server,
    store,
    input_adapter,
    agent_pool_map=None,
    pool_session_store=None,
    workspace_root: Path | None = None,
) -> None:
    agent_pool_map = agent_pool_map or {"main": "main", "coding": "coding"}
    pipe = build_webui_pipeline(
        skill_registry=_NoSkillRegistry(), known_pools=set(agent_pool_map)
    )
    if pool_session_store is None:
        pool_session_store = MagicMock()
        pool_session_store.get = lambda key, default=None: default
        pool_session_store.set = MagicMock()
    # PersistUserMessageStage (S7) routes its append by the bound workspace root
    # (ctxvar), and ResolveWorkspaceStage stamps it from ctx.current_ws(). When a
    # test supplies a workspace_root, route the resolved workspace there so the
    # user-message append lands under <workspace_root>/.modex/sessions — the same
    # directory the server's home_sessions_dir points at.
    current_ws_provider: Callable[[], Path]
    if workspace_root is not None:
        current_ws_provider = (lambda root=workspace_root: root)
    else:
        from pathlib import Path as _Path

        current_ws_provider = (lambda: _Path.cwd())
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=pool_session_store,
        agent_pool_map=agent_pool_map,
        agent_resolver=lambda p: agent_pool_map.get(p, p),
        transcript_store=store,
        enqueue_message=input_adapter.put_input_message,
        command_adapter=input_adapter,
        current_ws_provider=current_ws_provider,
    )
    server.set_input_pipeline(pipe)
    server.set_input_context(ctx)
