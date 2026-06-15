from __future__ import annotations
from unittest.mock import MagicMock
from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import SkillRegistry, ParsedSkill

class _NoSkillRegistry(SkillRegistry):
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        return None

def attach_default_pipeline(server, store, input_adapter, agent_pool_map=None,
                            pool_session_store=None) -> None:
    agent_pool_map = agent_pool_map or {"main": "main", "coding": "coding"}
    pipe = build_webui_pipeline(
        skill_registry=_NoSkillRegistry(), known_pools=set(agent_pool_map)
    )
    if pool_session_store is None:
        pool_session_store = MagicMock()
        pool_session_store.get = lambda key, default=None: default
        pool_session_store.set = MagicMock()
    ctx = BotInputContext(
        default_pool="main",
        pool_session_store=pool_session_store,
        agent_pool_map=agent_pool_map,
        agent_resolver=lambda p: agent_pool_map.get(p, p),
        transcript_store=store,
        enqueue_message=input_adapter.put_input_message,
        command_adapter=input_adapter,
    )
    server.set_input_pipeline(pipe)
    server.set_input_context(ctx)
