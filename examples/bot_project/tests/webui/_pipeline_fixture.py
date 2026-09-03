from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bot.input_pipeline.assembly import build_webui_pipeline
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg

from tests.input_pipeline.assembly_support import (
    TEST_ASSEMBLY_CTX,
    TEST_COMPONENT_REGISTRY,
)


def _no_skill_resolvers() -> PoolSkillResolverRegistry:
    return PoolSkillResolverRegistry(lambda _workspace, _pool: None)


def _bot_model_config() -> BotModelConfig:
    return BotModelConfig(
        default_provider="A",
        default_model="M1",
        providers=[
            ProviderCfg(
                key="a", name="A", url="u", api_key="k",
                models=[ModelCfg(name="M1", model="m1")],
            )
        ],
    )


async def attach_default_pipeline(
    server,
    store,
    input_adapter,
    pool_session_store=None,
    workspace_root: Path | None = None,
    available_pools: Callable[[], set[str]] | None = None,
) -> None:
    pipe = await build_webui_pipeline(
        registry=TEST_COMPONENT_REGISTRY,
        ctx=TEST_ASSEMBLY_CTX,
        skill_registry=_no_skill_resolvers(), bot_model_config=_bot_model_config()
    )
    if pool_session_store is None:
        pool_session_store = MagicMock()
        pool_session_store.get = lambda key, default=None: default
        pool_session_store.set = MagicMock()
    transcript_store = MagicMock(wraps=store)
    transcript_store.append = AsyncMock(side_effect=store.append)
    # PersistUserMessageStage (S7) routes its append by the bound workspace root
    # (ctxvar), and ResolveWorkspaceStage stamps it from ctx.current_ws(). When a
    # test supplies a workspace_root, route the resolved workspace there so the
    # user-message append lands under <workspace_root>/.modex/sessions — the same
    # directory the server's home_sessions_dir points at.
    current_ws_provider: Callable[[], Path]
    if workspace_root is not None:
        def current_ws_provider(root=workspace_root):
            return root
    else:
        from pathlib import Path as _Path

        def current_ws_provider():
            return _Path.cwd()
    ctx = BotInputContext(
        default_pool="main",
        available_pools=available_pools or (lambda: {"main", "coding"}),
        pool_session_store=pool_session_store,
        agent_resolver=lambda p: p,
        transcript_store=transcript_store,
        enqueue_message=input_adapter.put_input_message,
        command_adapter=input_adapter,
        current_ws_provider=current_ws_provider,
    )
    server.set_input_pipeline(pipe)
    server.set_input_context(ctx)
