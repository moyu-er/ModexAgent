from __future__ import annotations

import pytest

from framework.approval.types import ApprovalAction
from framework.commands.constants import (
    BuiltinCommand,
    CommandAction,
    CommandDispatchPolicy,
)
from framework.commands.handlers import (
    ApprovalCommandHandler,
    ContinueCommandHandler,
    InvalidCommandHandler,
    UnknownCommandHandler,
)
from framework.commands.models import CommandContext, SlashCommandInvocation
from framework.core.types import InputMessage


def _context(content: str = "/continue", *, pending: object | None = None) -> CommandContext:
    return CommandContext(
        session_id="s1",
        input_msg=InputMessage(content=content, session_id="s1"),
        agent_name="main",
        pending_approval=pending,
    )


@pytest.mark.asyncio
async def test_continue_triggers_agent_without_user_message() -> None:
    handler = ContinueCommandHandler()
    invocation = SlashCommandInvocation(
        command="continue",
        args="extra text",
        raw="/continue extra text",
    )
    result = await handler.handle(invocation, _context("/continue extra text"))
    assert result.action == CommandAction.CONTINUE_AGENT
    assert result.dispatch_policy == CommandDispatchPolicy.NORMAL_QUEUE
    assert result.trigger_agent is True
    assert result.append_user_message is False
    assert result.user_content is None


@pytest.mark.asyncio
async def test_continue_blocked_by_pending_approval() -> None:
    handler = ContinueCommandHandler()
    invocation = SlashCommandInvocation(command="continue", args="", raw="/continue")
    result = await handler.handle(invocation, _context(pending=object()))
    assert result.action == CommandAction.NOTICE
    assert result.trigger_agent is False
    assert result.notice is not None


@pytest.mark.asyncio
async def test_approval_without_pending_returns_notice() -> None:
    handler = ApprovalCommandHandler()
    invocation = SlashCommandInvocation(
        command=BuiltinCommand.APPROVE.value,
        args="ignored",
        raw="/approve ignored",
    )
    result = await handler.handle(invocation, _context("/approve ignored"))
    assert result.action == CommandAction.NOTICE
    assert result.notice is not None


@pytest.mark.asyncio
async def test_approval_with_pending_returns_decision() -> None:
    handler = ApprovalCommandHandler()
    invocation = SlashCommandInvocation(command=BuiltinCommand.DENY.value, args="", raw="/deny")
    result = await handler.handle(invocation, _context("/deny", pending=object()))
    assert result.action == CommandAction.APPROVAL_DECISION
    assert result.dispatch_policy == CommandDispatchPolicy.APPROVAL_RESPONSE
    assert result.approval_action == ApprovalAction.DENY


@pytest.mark.asyncio
async def test_unknown_command_returns_notice() -> None:
    result = await UnknownCommandHandler().handle(
        SlashCommandInvocation(command="missing", args="", raw="/missing"),
        _context("/missing"),
    )
    assert result.action == CommandAction.NOTICE
    assert "Unknown command: /missing" in (result.notice or "")


@pytest.mark.asyncio
async def test_invalid_command_returns_notice() -> None:
    result = await InvalidCommandHandler().handle_parse_error("bad syntax", _context("/Bad"))
    assert result.action == CommandAction.NOTICE
    assert "Invalid command syntax" in (result.notice or "")
