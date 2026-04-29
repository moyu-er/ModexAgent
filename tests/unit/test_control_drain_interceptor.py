"""Tests for ControlDrainInterceptor — cancel commands at turn/iteration boundaries."""

from __future__ import annotations

import pytest

from framework.control.channel import InMemoryControlChannel
from framework.control.exceptions import AgentCancelled
from framework.control.types import (
    ControlCommand,
    ControlCommandType,
    ControlScope,
)
from framework.core.emitter import AgentResult
from framework.interceptor.abc import IterationContext
from framework.interceptor.builtin.control_drain import ControlDrainInterceptor


class FakeCtx:
    def __init__(self, session_id: str = "s1") -> None:
        self.session_id = session_id
        self.metadata = {}


@pytest.fixture
def channel():
    return InMemoryControlChannel()


@pytest.fixture
def fake_ctx():
    return FakeCtx()


class TestControlDrainCancelTurn:
    """ControlDrainInterceptor around_turn drains CANCEL_RUN / CANCEL_TURN commands."""

    @pytest.mark.asyncio
    async def test_cancel_run_raises(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel)
        await channel.send(
            ControlCommand(
                command_id="cmd-1",
                type=ControlCommandType.CANCEL_RUN,
                scope=ControlScope(session_id="s1"),
                payload={"reason": "admin cancel"},
            )
        )

        async def next_call() -> AgentResult:
            return AgentResult(content="ok")

        with pytest.raises(AgentCancelled):
            await interceptor.around_turn(fake_ctx, next_call)

    @pytest.mark.asyncio
    async def test_cancel_turn_raises(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel)
        await channel.send(
            ControlCommand(
                command_id="cmd-1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
                payload={"reason": "user cancel"},
            )
        )

        async def next_call() -> AgentResult:
            return AgentResult(content="ok")

        with pytest.raises(AgentCancelled):
            await interceptor.around_turn(fake_ctx, next_call)

    @pytest.mark.asyncio
    async def test_no_command_allows_turn(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel)

        async def next_call() -> AgentResult:
            return AgentResult(content="ok")

        result = await interceptor.around_turn(fake_ctx, next_call)
        assert result.content == "ok"

    @pytest.mark.asyncio
    async def test_different_session_not_affected(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel)
        await channel.send(
            ControlCommand(
                command_id="cmd-1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s2"),
            )
        )

        async def next_call() -> AgentResult:
            return AgentResult(content="ok")

        # s1 should not be affected by s2's cancel
        result = await interceptor.around_turn(fake_ctx, next_call)
        assert result.content == "ok"


class TestControlDrainCancelIteration:
    """ControlDrainInterceptor around_iteration drains cancel commands between iterations."""

    @pytest.mark.asyncio
    async def test_cancel_at_iteration_boundary(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel)
        await channel.send(
            ControlCommand(
                command_id="cmd-1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
            )
        )

        async def next_call() -> None:
            pass

        with pytest.raises(AgentCancelled):
            await interceptor.around_iteration(
                fake_ctx, IterationContext(iteration=2, turn_id="t1"), next_call
            )

    @pytest.mark.asyncio
    async def test_iteration_continues_without_command(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel)
        called = False

        async def next_call() -> None:
            nonlocal called
            called = True

        await interceptor.around_iteration(
            fake_ctx, IterationContext(iteration=1, turn_id="t1"), next_call
        )
        assert called

    @pytest.mark.asyncio
    async def test_max_commands_limits_drain(self, channel, fake_ctx):
        interceptor = ControlDrainInterceptor(channel=channel, max_commands=1)
        await channel.send(
            ControlCommand(
                command_id="c1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
            )
        )
        # Second command should remain in channel
        await channel.send(
            ControlCommand(
                command_id="c2",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
            )
        )

        async def next_call() -> AgentResult:
            return AgentResult(content="ok")

        with pytest.raises(AgentCancelled):
            await interceptor.around_turn(fake_ctx, next_call)

        # c2 should still be in channel (limit=1 consumed only c1)
        remaining = await channel.peek(ControlScope(session_id="s1"))
        assert len(remaining) == 1
        assert remaining[0].command_id == "c2"
