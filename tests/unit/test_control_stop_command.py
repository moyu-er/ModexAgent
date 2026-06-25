"""Tests for ControlCommandHandler — /stop slash command."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from modex_agent.commands.constants import CommandAction, CommandDispatchPolicy
from modex_agent.commands.handlers import ControlCommandHandler
from modex_agent.commands.models import CommandContext, SlashCommandInvocation
from modex_agent.control.types import ControlCommandType


def _make_invocation(command: str = "stop", args: str = "") -> SlashCommandInvocation:
    return SlashCommandInvocation(command=command, args=args, raw=f"/{command} {args}".strip())


def _make_context(session_id: str = "test-session") -> CommandContext:
    return CommandContext(
        session_id=session_id,
        input_msg=MagicMock(),
        agent_name="main",
    )


class TestControlCommandHandler:

    def test_names_includes_stop(self):
        handler = ControlCommandHandler()
        assert "stop" in handler.names

    def test_dispatch_policy_is_bypass_queue(self):
        handler = ControlCommandHandler()
        inv = _make_invocation("stop")
        ctx = _make_context()
        assert handler.dispatch_policy(inv, ctx) == CommandDispatchPolicy.BYPASS_QUEUE

    @pytest.mark.asyncio
    async def test_stop_returns_control_command_action(self):
        handler = ControlCommandHandler()
        inv = _make_invocation("stop")
        ctx = _make_context(session_id="sess-123")
        result = await handler.handle(inv, ctx)

        assert result.action == CommandAction.CONTROL_COMMAND
        assert result.dispatch_policy == CommandDispatchPolicy.BYPASS_QUEUE
        assert result.control_command is not None
        assert result.control_command.type == ControlCommandType.CANCEL_TURN
        assert result.control_command.scope.session_id == "sess-123"
        assert result.control_command.source == "user:slash"
        assert result.notice is not None
        assert "stop" in result.notice.lower()

    @pytest.mark.asyncio
    async def test_stop_with_no_running_agent_is_ok(self):
        """Handler itself doesn't check for running agents — it just returns the command."""
        handler = ControlCommandHandler()
        inv = _make_invocation("stop")
        ctx = _make_context()
        result = await handler.handle(inv, ctx)
        # No exception — handler is pure, pipeline handles the actual cancel
        assert result.action == CommandAction.CONTROL_COMMAND

    def test_unknown_command_not_in_names(self):
        handler = ControlCommandHandler()
        assert "pause" not in handler.names
        assert "resume" not in handler.names
