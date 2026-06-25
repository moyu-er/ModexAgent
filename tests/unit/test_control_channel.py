"""Tests for InMemoryControlChannel — TTL, drain, peek, scope matching."""

from __future__ import annotations

import asyncio

import pytest

from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.control.types import ControlCommand, ControlCommandType, ControlScope


@pytest.fixture
def channel():
    return InMemoryControlChannel()


class TestControlChannelDrain:
    """drain() consumes scoped commands and returns them."""

    @pytest.mark.asyncio
    async def test_drain_returns_matching_commands(self, channel):
        await channel.send(
            ControlCommand(
                command_id="c1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
            )
        )
        await channel.send(
            ControlCommand(
                command_id="c2",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s2"),
            )
        )

        result = await channel.drain(ControlScope(session_id="s1"))
        assert len(result) == 1
        assert result[0].command_id == "c1"

        # s2 command still present
        result2 = await channel.drain(ControlScope(session_id="s2"))
        assert len(result2) == 1
        assert result2[0].command_id == "c2"

    @pytest.mark.asyncio
    async def test_drain_respects_limit(self, channel):
        for i in range(5):
            await channel.send(
                ControlCommand(
                    command_id=f"c{i}",
                    type=ControlCommandType.CANCEL_TURN,
                    scope=ControlScope(session_id="s1"),
                )
            )

        result = await channel.drain(ControlScope(session_id="s1"), limit=2)
        assert len(result) == 2
        # Remaining 3 still drainable
        result2 = await channel.drain(ControlScope(session_id="s1"))
        assert len(result2) == 3

    @pytest.mark.asyncio
    async def test_drain_with_agent_id_filter(self, channel):
        await channel.send(
            ControlCommand(
                command_id="c1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1", agent_id="a1"),
            )
        )
        await channel.send(
            ControlCommand(
                command_id="c2",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1", agent_id="a2"),
            )
        )

        # Target specific agent
        result = await channel.drain(ControlScope(session_id="s1", agent_id="a1"))
        assert len(result) == 1
        assert result[0].command_id == "c1"

        # No agent_id restriction → matches both remaining
        result2 = await channel.drain(ControlScope(session_id="s1"))
        assert len(result2) == 1
        assert result2[0].command_id == "c2"


class TestControlChannelTTL:
    """Expired commands must be discarded during drain."""

    @pytest.mark.asyncio
    async def test_expired_command_is_discarded(self, channel):
        await channel.send(
            ControlCommand(
                command_id="expired",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
                ttl_seconds=0.01,
            )
        )
        await asyncio.sleep(0.05)

        result = await channel.drain(ControlScope(session_id="s1"))
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_fresh_command_is_returned(self, channel):
        await channel.send(
            ControlCommand(
                command_id="fresh",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
                ttl_seconds=60.0,
            )
        )

        result = await channel.drain(ControlScope(session_id="s1"))
        assert len(result) == 1
        assert result[0].command_id == "fresh"

    @pytest.mark.asyncio
    async def test_mixed_expired_and_fresh(self, channel):
        await channel.send(
            ControlCommand(
                command_id="expired",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
                ttl_seconds=0.01,
            )
        )
        await asyncio.sleep(0.05)
        await channel.send(
            ControlCommand(
                command_id="fresh",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
                ttl_seconds=60.0,
            )
        )

        result = await channel.drain(ControlScope(session_id="s1"))
        assert len(result) == 1
        assert result[0].command_id == "fresh"


class TestControlChannelPeek:
    """peek() is non-destructive."""

    @pytest.mark.asyncio
    async def test_peek_does_not_consume(self, channel):
        await channel.send(
            ControlCommand(
                command_id="c1",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="s1"),
            )
        )

        result1 = await channel.peek(ControlScope(session_id="s1"))
        assert len(result1) == 1

        # Command still there after peek
        result2 = await channel.drain(ControlScope(session_id="s1"))
        assert len(result2) == 1

        # Nothing left after drain
        result3 = await channel.peek(ControlScope(session_id="s1"))
        assert len(result3) == 0
