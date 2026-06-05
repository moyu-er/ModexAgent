from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.commands.constants import CommandAction, CommandDispatchPolicy
from framework.commands.models import CommandContext, SlashCommandInvocation
from framework.workspace.handlers import CdCommandHandler, ExitCommandHandler, PwdCommandHandler
from framework.workspace.models import CdResult


def _make_invocation(command: str, args: str = "") -> SlashCommandInvocation:
    return SlashCommandInvocation(
        command=command, args=args, raw=f"/{command} {args}".strip()
    )


def _make_context() -> CommandContext:
    from framework.core.types import InputMessage

    return CommandContext(
        session_id="test-session",
        input_msg=InputMessage(content="/cd /tmp", session_id="test-session"),
        agent_name="main",
    )


class TestCdCommandHandler:
    def test_names(self) -> None:
        handler = CdCommandHandler(workspace_ctx=MagicMock())
        assert "cd" in handler.names

    def test_dispatch_policy_is_normal_queue(self) -> None:
        handler = CdCommandHandler(workspace_ctx=MagicMock())
        policy = handler.dispatch_policy(_make_invocation("cd", "/tmp"), _make_context())
        assert policy == CommandDispatchPolicy.NORMAL_QUEUE

    @pytest.mark.asyncio
    async def test_handle_cd_success(self) -> None:
        mock_ctx = AsyncMock()
        mock_ctx.cd.return_value = CdResult(
            success=True,
            current_path=Path("/tmp"),
            original_path=Path("/home"),
            notice="switched to: /tmp",
        )
        handler = CdCommandHandler(workspace_ctx=mock_ctx)
        result = await handler.handle(_make_invocation("cd", "/tmp"), _make_context())
        assert result.action == CommandAction.NOTICE
        assert "switched" in result.notice
        mock_ctx.cd.assert_called_once_with("/tmp")

    @pytest.mark.asyncio
    async def test_handle_cd_failure(self) -> None:
        mock_ctx = AsyncMock()
        mock_ctx.cd.return_value = CdResult(
            success=False,
            current_path=Path("/home"),
            original_path=Path("/home"),
            notice="cd: path not found: '/nonexist'",
            error="path_not_found",
        )
        handler = CdCommandHandler(workspace_ctx=mock_ctx)
        result = await handler.handle(_make_invocation("cd", "/nonexist"), _make_context())
        assert result.action == CommandAction.NOTICE
        assert "path" in result.notice

    @pytest.mark.asyncio
    async def test_handle_cd_no_args(self) -> None:
        mock_ctx = AsyncMock()
        handler = CdCommandHandler(workspace_ctx=mock_ctx)
        result = await handler.handle(_make_invocation("cd", ""), _make_context())
        assert result.action == CommandAction.NOTICE
        assert "path" in result.notice


class TestExitCommandHandler:
    def test_names(self) -> None:
        handler = ExitCommandHandler(workspace_ctx=MagicMock())
        assert "exit" in handler.names

    def test_dispatch_policy_is_normal_queue(self) -> None:
        handler = ExitCommandHandler(workspace_ctx=MagicMock())
        policy = handler.dispatch_policy(_make_invocation("exit"), _make_context())
        assert policy == CommandDispatchPolicy.NORMAL_QUEUE

    @pytest.mark.asyncio
    async def test_handle_exit_success(self) -> None:
        mock_ctx = AsyncMock()
        mock_ctx.exit.return_value = CdResult(
            success=True,
            current_path=Path("/home"),
            original_path=Path("/home"),
            notice="returned to home: /home",
        )
        handler = ExitCommandHandler(workspace_ctx=mock_ctx)
        result = await handler.handle(_make_invocation("exit"), _make_context())
        assert result.action == CommandAction.NOTICE
        assert "home" in result.notice

    @pytest.mark.asyncio
    async def test_handle_exit_already_home(self) -> None:
        mock_ctx = AsyncMock()
        mock_ctx.exit.return_value = CdResult(
            success=False,
            current_path=Path("/home"),
            original_path=Path("/home"),
            notice="exit: already at home",
            error="already_home",
        )
        handler = ExitCommandHandler(workspace_ctx=mock_ctx)
        result = await handler.handle(_make_invocation("exit"), _make_context())
        assert result.action == CommandAction.NOTICE
        assert "already at home" in result.notice


class TestPwdCommandHandler:
    def test_names(self) -> None:
        handler = PwdCommandHandler(workspace_ctx=MagicMock())
        assert "pwd" in handler.names

    def test_dispatch_policy_is_normal_queue(self) -> None:
        handler = PwdCommandHandler(workspace_ctx=MagicMock())
        policy = handler.dispatch_policy(_make_invocation("pwd"), _make_context())
        assert policy == CommandDispatchPolicy.NORMAL_QUEUE

    @pytest.mark.asyncio
    async def test_handle_pwd(self) -> None:
        ctx = MagicMock()
        ctx.current = Path("/home/user/project")
        ctx.home = Path("/home/user/startup")
        handler = PwdCommandHandler(workspace_ctx=ctx)
        result = await handler.handle(_make_invocation("pwd"), _make_context())
        assert result.action == CommandAction.NOTICE
        assert "project" in result.notice
        assert "startup" in result.notice
