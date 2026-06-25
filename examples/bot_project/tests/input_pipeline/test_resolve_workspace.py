from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from modex_agent.input_pipeline.envelope import UserInputEnvelope


def _ctx(current_ws_provider=None) -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        pool_session_store=MagicMock(),
        agent_pool_map={"main": "main"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
        current_ws_provider=current_ws_provider,
    )


@pytest.mark.asyncio
async def test_resolve_workspace_prefers_envelope_metadata() -> None:
    """When envelope.metadata["workspace"] is set (WebUI wire payload in Task 5),
    the stage uses it verbatim."""
    ws = Path("/srv/workspaces/foo")
    env = UserInputEnvelope(external_id="u1", content="hi", channel="websocket")
    env.metadata["workspace"] = str(ws)
    result = await ResolveWorkspaceStage().process(env, _ctx())
    assert result.should_continue()
    assert Path(env.metadata[RoutingMeta.WORKSPACE]) == ws


@pytest.mark.asyncio
async def test_resolve_workspace_falls_back_to_provider() -> None:
    """Without an explicit metadata workspace, the stage resolves from the
    ctx.current_ws() provider (default home in Task 1)."""
    ws = Path("/srv/workspaces/bar")
    env = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
    provider = MagicMock(return_value=ws)
    await ResolveWorkspaceStage().process(env, _ctx(current_ws_provider=provider))
    provider.assert_called_once()
    assert env.metadata[RoutingMeta.WORKSPACE] == str(ws)


@pytest.mark.asyncio
async def test_resolve_workspace_default_provider_is_home() -> None:
    """The default provider returns Path.cwd() (home)."""
    env = UserInputEnvelope(external_id="u1", content="hi", channel="qq")
    await ResolveWorkspaceStage().process(env, _ctx())
    assert env.metadata[RoutingMeta.WORKSPACE] == str(Path.cwd())
