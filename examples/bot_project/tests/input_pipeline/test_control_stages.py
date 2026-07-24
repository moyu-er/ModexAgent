from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.input_pipeline.stages.session_control import SessionControlStage
from modex_agent.workspace.control import WorkspaceController
from modex_agent.core.session_id import SessionIdFactory, encode_snowflake
from modex_agent.core.types import InputMessage
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.workspace.models import CdResult


def _sid(agent: str, conv: str) -> str:
    return SessionIdFactory().create(agent_name=agent, external_id=conv).session_id


def _ctx(
    *,
    store_get: str = "main",
    command_handled: bool = False,
    current_ws: Path | None = None,
    home: Path | None = None,
) -> BotInputContext:
    store = MagicMock()
    store.get.return_value = store_get

    home_dir = home or Path("/project")
    ws = current_ws or home_dir

    # Use a simple class instead of MagicMock so attribute assignment works
    class _FakeAdapter:
        name = "qq"
        current_ws: Path = ws
        home: Path = home_dir
        _called: bool = False

        def save_current_ws(self) -> None:
            pass

        async def _try_intercept_control(self, text: str, session_id: str) -> bool:
            self._called = True
            return command_handled

    cmd_adapter = _FakeAdapter()
    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main", "coding"},
        pool_session_store=store,
        agent_pool_map={"main": "main", "coding": "coding"},
        agent_resolver=lambda p: p,
        transcript_store=MagicMock(),
        enqueue_message=MagicMock(),
        command_adapter=cmd_adapter,  # type: ignore[arg-type]
        current_ws_provider=lambda: cmd_adapter.current_ws,
    )


def _mock_controller(home: Path) -> WorkspaceController:
    """Return a mock WorkspaceController that delegates to a real open_workspace."""
    controller = MagicMock(spec=WorkspaceController)
    controller.home = home

    async def _open_workspace(target: str) -> CdResult:
        resolved = (home / target).resolve()
        if not resolved.exists():
            return CdResult(
                success=False,
                current_path=home,
                original_path=home,
                notice=f"cd: path not found: '{target}'",
            )
        if not resolved.is_dir():
            return CdResult(
                success=False,
                current_path=home,
                original_path=home,
                notice=f"cd: not a directory: '{target}'",
            )
        return CdResult(
            success=True,
            current_path=resolved,
            original_path=home,
            notice=f"cd: workspace ready at {resolved}",
        )

    controller.open_workspace = AsyncMock(side_effect=_open_workspace)
    return controller  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_pool_command_terminates_and_records_pool() -> None:
    """S2: /pool shortcut (e.g. /coding) records pool and terminates."""
    ctx = _ctx()
    env = UserInputEnvelope(external_id="u1", content="/coding", channel="qq")
    stage = EnvironmentControlStage(known_pools={"coding"})
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    ctx.pool_session_store.set.assert_called_once_with(encode_snowflake("u1"), "coding")


@pytest.mark.asyncio
async def test_normal_message_passes_through() -> None:
    """S2: ordinary text passes through to next stage."""
    ctx = _ctx()
    env = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is True


@pytest.mark.asyncio
async def test_environment_stage_does_not_handle_stop() -> None:
    """S2: /stop passes through — S3 owns it."""
    ctx = _ctx(command_handled=True)
    env = UserInputEnvelope(external_id="u1", content="/stop", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is True
    assert ctx.command_adapter._called is False


@pytest.mark.asyncio
async def test_stop_command_handled_by_session_stage() -> None:
    """S3: /stop resolves full_session_id and delegates to adapter."""
    ctx = _ctx(store_get="coding", command_handled=True)
    env = UserInputEnvelope(external_id="u1", content="/stop", channel="qq")
    stage = SessionControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    assert ctx.command_adapter._called is True


@pytest.mark.asyncio
async def test_continue_command_enqueues_continue_signal() -> None:
    """CommandDispatchStage: /continue enqueues a continue InputMessage and marks HANDLED."""
    from bot.input_pipeline.stages.command import CommandDispatchStage
    from bot.input_pipeline.stages.commands import SHARED_COMMANDS
    from modex_agent.input_pipeline.envelope import CommandStatus

    enqueued: list[InputMessage] = []
    ctx = _ctx(store_get="main")
    ctx._enqueue_message = MagicMock(side_effect=enqueued.append)
    ctx.enqueue_message = MagicMock(side_effect=enqueued.append)  # type: ignore[method-assign]
    env = UserInputEnvelope(external_id="u1", content="/continue", channel="qq")
    stage = CommandDispatchStage(handlers=SHARED_COMMANDS)
    result = await stage.process(env, ctx)

    assert result.should_continue() is True
    assert env.command_status is CommandStatus.HANDLED
    assert len(enqueued) == 1
    assert enqueued[0].content == "/continue"
    assert enqueued[0].session.session_id.endswith(".main")


@pytest.mark.asyncio
async def test_session_stage_passes_non_stop() -> None:
    """S3: non-/stop messages pass through."""
    ctx = _ctx(command_handled=True)
    env = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
    stage = SessionControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is True
    assert ctx.command_adapter._called is False


@pytest.mark.asyncio
async def test_cd_valid_dir_updates_current_ws_and_persists(tmp_path: Path) -> None:
    """S2: /cd <valid-dir> updates adapter.current_ws, persists, and terminates."""
    home = tmp_path / "project"
    home.mkdir()
    target_dir = home / "workspace"
    target_dir.mkdir()

    ctx = _ctx(home=home)
    env = UserInputEnvelope(external_id="u1", content=f"/cd {target_dir.name}", channel="qq")
    controller = _mock_controller(home)
    stage = EnvironmentControlStage(workspace_controller=controller)
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    assert ctx.command_adapter.current_ws == target_dir

    # Verify persistence was called (save_current_ws is a no-op on _FakeAdapter,
    # but the real adapter would persist; here we just verify the controller was called)
    controller.open_workspace.assert_awaited_once_with(target_dir.name)


@pytest.mark.asyncio
async def test_cd_invalid_dir_terminates_with_error_and_does_not_change_ws(tmp_path: Path) -> None:
    """S2: /cd <invalid-dir> terminates with error and does not change current_ws."""
    home = tmp_path / "project"
    home.mkdir()
    original_ws = home

    ctx = _ctx(home=home, current_ws=original_ws)
    env = UserInputEnvelope(external_id="u1", content="/cd nonexistent", channel="qq")
    controller = _mock_controller(home)
    stage = EnvironmentControlStage(workspace_controller=controller)
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    assert ctx.command_adapter.current_ws == original_ws

    # Check error message
    response = getattr(result, "response", None)
    assert response is not None
    assert "not found" in response["message"].lower() or "invalid" in response["message"].lower()


@pytest.mark.asyncio
async def test_exit_resets_current_ws_to_home(tmp_path: Path) -> None:
    """S2: /exit resets current_ws to home and terminates."""
    home = tmp_path / "project"
    home.mkdir()
    workspace_dir = home / "workspace"
    workspace_dir.mkdir()

    ctx = _ctx(home=home, current_ws=workspace_dir)
    env = UserInputEnvelope(external_id="u1", content="/exit", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    assert ctx.command_adapter.current_ws == home


@pytest.mark.asyncio
async def test_pwd_returns_current_workspace(tmp_path: Path) -> None:
    """S2: /pwd terminates with current workspace notice."""
    home = tmp_path / "project"
    home.mkdir()
    workspace_dir = home / "workspace"
    workspace_dir.mkdir()

    ctx = _ctx(home=home, current_ws=workspace_dir)
    env = UserInputEnvelope(external_id="u1", content="/pwd", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    response = getattr(result, "response", None)
    assert response is not None
    assert str(workspace_dir) in response["message"]
    assert "home:" not in response["message"].lower()


@pytest.mark.asyncio
async def test_subsequent_message_workspace_follows_cd(tmp_path: Path) -> None:
    """A message after /cd has workspace metadata set to the new current_ws."""
    from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage

    home = tmp_path / "project"
    home.mkdir()
    workspace_dir = home / "workspace"
    workspace_dir.mkdir()

    # First, /cd to workspace
    ctx = _ctx(home=home)
    env = UserInputEnvelope(external_id="u1", content=f"/cd {workspace_dir.name}", channel="qq")
    controller = _mock_controller(home)
    stage = EnvironmentControlStage(workspace_controller=controller)
    await stage.process(env, ctx)

    # Now a normal message — ResolveWorkspaceStage should pick up the new current_ws
    ctx2 = _ctx(home=home, current_ws=ctx.command_adapter.current_ws)
    env2 = UserInputEnvelope(external_id="u1", content="hello", channel="qq")
    ws_stage = ResolveWorkspaceStage()
    result = await ws_stage.process(env2, ctx2)

    assert result.should_continue() is True
    assert env2.metadata[RoutingMeta.WORKSPACE] == str(workspace_dir)


@pytest.mark.asyncio
async def test_other_command_delegates_to_adapter() -> None:
    """S2: commands other than /cd/exit/pwd/stop/pool still delegate to _try_intercept_control."""
    ctx = _ctx(command_handled=True)
    # Use /123foo (starts with digit) so it doesn't match the pool regex ^/([a-z][a-z0-9_-]*)$
    env = UserInputEnvelope(external_id="u1", content="/123foo", channel="qq")
    stage = EnvironmentControlStage()
    result = await stage.process(env, ctx)

    assert result.should_continue() is False
    # The _try_intercept_control on the fake adapter was called
    assert ctx.command_adapter._called is True
