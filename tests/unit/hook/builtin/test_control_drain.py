"""Tests for drain_control_channel() and control interceptors."""

from __future__ import annotations

import pytest

from framework.control.channel import InMemoryControlChannel
from framework.control.types import ControlCommand, ControlCommandType, ControlScope
from framework.control.exceptions import AgentCancelled
from framework.core.session_id import SessionInfo
from framework.hook.builtin.control_drain import (
    ControlDrainInterceptor,
    LlmCancelInterceptor,
    drain_control_channel,
)


class _FakeContext:
    def __init__(self, session_id="test-session.main", turn_uuid=None):
        from framework.core.session_id import SessionInfo
        self.session = SessionInfo.from_str(session_id)
        self.current_turn_uuid = turn_uuid


class TestDrainControlChannel:

    @pytest.mark.asyncio
    async def test_no_channel_returns_false(self):
        ctx = _FakeContext()
        result = await drain_control_channel(None, ctx)
        assert result is False

    @pytest.mark.asyncio
    async def test_empty_channel_returns_false(self):
        channel = InMemoryControlChannel()
        ctx = _FakeContext()
        result = await drain_control_channel(channel, ctx)
        assert result is False

    @pytest.mark.asyncio
    async def test_matching_turn_uuid_raises_agent_cancelled(self):
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="test-1",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="sess-1:main"),
            payload={"turn_uuid": "abc123"},
        )
        await channel.send(cmd)
        ctx = _FakeContext(session_id="sess-1:main", turn_uuid="abc123")

        with pytest.raises(AgentCancelled) as exc:
            await drain_control_channel(channel, ctx, turn_uuid="abc123")
        assert "/stop" in str(exc.value)

    @pytest.mark.asyncio
    async def test_mismatched_turn_uuid_discards_silently(self):
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="test-2",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="sess-2:main"),
            payload={"turn_uuid": "old-uuid"},
        )
        await channel.send(cmd)
        ctx = _FakeContext(session_id="sess-2:main")

        result = await drain_control_channel(channel, ctx, turn_uuid="new-uuid")
        assert result is True  # consumed but no action
        # Verify command was drained (consumed)
        remaining = await channel.peek(ControlScope(session_id="sess-2:main"))
        assert len(remaining) == 0

    @pytest.mark.asyncio
    async def test_no_turn_uuid_in_payload_executes_anyway(self):
        """Backward compatible: if no turn_uuid in payload, execute the command."""
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="test-3",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="sess-3:main"),
        )
        await channel.send(cmd)
        ctx = _FakeContext(session_id="sess-3:main", turn_uuid="abc")

        with pytest.raises(AgentCancelled):
            await drain_control_channel(channel, ctx, turn_uuid="abc")

    @pytest.mark.asyncio
    async def test_multiple_commands_drained_at_once(self):
        channel = InMemoryControlChannel()
        for i in range(3):
            cmd = ControlCommand(
                command_id=f"test-{i}",
                type=ControlCommandType.CANCEL_TURN,
                scope=ControlScope(session_id="sess-multi:main"),
                payload={"turn_uuid": "match"},
            )
            await channel.send(cmd)
        ctx = _FakeContext(session_id="sess-multi:main", turn_uuid="match")

        with pytest.raises(AgentCancelled):
            await drain_control_channel(channel, ctx, turn_uuid="match")
        # All drained
        remaining = await channel.peek(ControlScope(session_id="sess-multi:main"))
        assert len(remaining) == 0


class TestControlDrainInterceptor:

    @pytest.mark.asyncio
    async def test_propagates_agent_cancelled(self):
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="int-1",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="sess-int:main"),
            payload={"turn_uuid": "turn-1"},
        )
        await channel.send(cmd)

        interceptor = ControlDrainInterceptor(channel=channel)
        ctx = _FakeContext(session_id="sess-int:main", turn_uuid="turn-1")

        # Create a minimal ToolCallContext
        from unittest.mock import MagicMock
        mock_call = MagicMock()
        mock_call.tool_call = MagicMock()
        mock_call.tool_call.call_id = "call-1"

        from unittest.mock import AsyncMock
        next_call = AsyncMock()

        with pytest.raises(AgentCancelled):
            await interceptor.around_tool_call(ctx, mock_call, next_call)

        # next_call should NOT have been called
        next_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_command_passes_through(self):
        channel = InMemoryControlChannel()
        interceptor = ControlDrainInterceptor(channel=channel)
        ctx = _FakeContext(session_id="sess-clean:main", turn_uuid="turn-1")

        from unittest.mock import MagicMock, AsyncMock
        mock_call = MagicMock()
        mock_call.tool_call = MagicMock()
        mock_call.tool_call.call_id = "call-1"

        mock_result = MagicMock()
        next_call = AsyncMock(return_value=mock_result)

        result = await interceptor.around_tool_call(ctx, mock_call, next_call)
        assert result is mock_result


class TestLlmCancelInterceptor:

    @pytest.mark.asyncio
    async def test_no_command_passes_through(self):
        channel = InMemoryControlChannel()
        interceptor = LlmCancelInterceptor(channel=channel)
        ctx = _FakeContext(session_id="sess-llm:main", turn_uuid="turn-1")

        from unittest.mock import MagicMock
        mock_call = MagicMock()

        async def _stream():
            yield MagicMock(content_delta="hello")
            yield MagicMock(content_delta=" world")

        chunks = []
        async for chunk in interceptor.around_llm_stream(ctx, mock_call, _stream):
            chunks.append(chunk)

        assert len(chunks) == 2

    @pytest.mark.asyncio
    async def test_cancel_command_mid_stream(self):
        """/stop during LLM streaming — AgentCancelled propagates immediately,
        aborting the stream before any chunks are yielded to the caller.
        This is a "hard cancel": the exception prevents subsequent tool
        calls from executing."""
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="llm-1",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="sess-llm-cancel:main"),
            payload={"turn_uuid": "t1"},
        )
        await channel.send(cmd)

        interceptor = LlmCancelInterceptor(channel=channel)
        ctx = _FakeContext(session_id="sess-llm-cancel:main", turn_uuid="t1")

        from unittest.mock import MagicMock
        mock_call = MagicMock()

        async def _stream():
            yield MagicMock(content_delta="chunk1")
            yield MagicMock(content_delta="chunk2")

        chunks = []
        with pytest.raises(AgentCancelled) as exc:
            async for chunk in interceptor.around_llm_stream(ctx, mock_call, _stream):
                chunks.append(chunk)

        assert "/stop" in str(exc.value)
        # No chunks yielded — the command is drained before the first yield
        assert len(chunks) == 0
        # Command was consumed (destructive drain)
        remaining = await channel.peek(ControlScope(session_id="sess-llm-cancel:main"))
        assert len(remaining) == 0


# ---------------------------------------------------------------------------
# End-to-end tests — canonical session_id + full producer→consumer flow
# ---------------------------------------------------------------------------


class TestCanonicalSessionId:
    """Verify SessionInfo.from_str() recovers agent_name from display strings."""

    def test_raw_user_id_defaults_agent_name(self):
        session = SessionInfo.from_str("30932BC02F825E64D069B1E67347C8FF")
        assert session.session_id == "30932BC02F825E64D069B1E67347C8FF"
        assert session.agent_name == "unknown"

    def test_raw_user_id_with_default_agent_name(self):
        session = SessionInfo.from_str(
            "30932BC02F825E64D069B1E67347C8FF", default_agent_name="main"
        )
        assert session.session_id == "30932BC02F825E64D069B1E67347C8FF"
        assert session.agent_name == "main"

    def test_canonical_parses_agent_name(self):
        session = SessionInfo.from_str("user.main")
        assert session.session_id == "user.main"
        assert session.agent_name == "main"
        assert session.snowflake == "user"


class TestEndToEndStopFlow:
    """Full producer→channel→consumer /stop flow with SessionInfo objects."""

    @pytest.mark.asyncio
    async def test_producer_adapter_id_consumer_agent_id_match(self):
        """Producer and consumer use the same session_id — SessionInfo always
        carries canonical form, so no normalize() step is needed."""
        channel = InMemoryControlChannel()

        raw_sid = "30932BC02F825E64D069B1E67347C8FF"
        canonical = f"{raw_sid}.main"

        cmd = ControlCommand(
            command_id="e2e-1",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id=canonical),
        )
        await channel.send(cmd)

        # Consumer side: same session_id in canonical form
        ctx = _FakeContext(session_id=canonical, turn_uuid="t1")

        with pytest.raises(AgentCancelled):
            await drain_control_channel(channel, ctx, turn_uuid="t1")

    @pytest.mark.asyncio
    async def test_dedup_works_with_canonical_ids(self):
        """Dedup via peek() uses SessionInfo canonical form."""
        channel = InMemoryControlChannel()
        raw_sid = "user123"
        canonical = f"{raw_sid}.main"

        cmd = ControlCommand(
            command_id="e2e-2",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id=canonical),
        )
        await channel.send(cmd)

        # Second send with same canonical ID → dedup
        existing = await channel.peek(
            ControlScope(session_id=canonical),
            command_types={ControlCommandType.CANCEL_TURN},
        )
        assert len(existing) == 1


class TestAllConsumersIndependentlyStop:
    """Verify each consumer (safe point + interceptor) can independently stop."""

    @pytest.mark.asyncio
    async def test_direct_drain_stops(self):
        """Simulates safe point 1-4: direct drain_control_channel call."""
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="c1", type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="s:main"),
        )
        await channel.send(cmd)
        ctx = _FakeContext(session_id="s:main", turn_uuid="t1")
        with pytest.raises(AgentCancelled):
            await drain_control_channel(channel, ctx, turn_uuid="t1")

    @pytest.mark.asyncio
    async def test_tool_interceptor_stops(self):
        """Simulates ControlDrainInterceptor before tool call."""
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="c2", type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="s:main"),
        )
        await channel.send(cmd)
        interceptor = ControlDrainInterceptor(channel=channel)
        ctx = _FakeContext(session_id="s:main", turn_uuid="t1")
        from unittest.mock import MagicMock, AsyncMock
        call = MagicMock(); call.tool_call = MagicMock(); call.tool_call.call_id = "c1"
        next_call = AsyncMock()
        with pytest.raises(AgentCancelled):
            await interceptor.around_tool_call(ctx, call, next_call)
        next_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_stream_interceptor_stops(self):
        """Simulates LlmCancelInterceptor during LLM streaming."""
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="c3", type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="s:main"),
        )
        await channel.send(cmd)
        interceptor = LlmCancelInterceptor(channel=channel)
        ctx = _FakeContext(session_id="s:main", turn_uuid="t1")
        from unittest.mock import MagicMock
        call = MagicMock()

        async def _stream():
            yield MagicMock(content_delta="c1")
            yield MagicMock(content_delta="c2")

        chunks = []
        with pytest.raises(AgentCancelled):
            async for chunk in interceptor.around_llm_stream(ctx, call, _stream):
                chunks.append(chunk)
        assert len(chunks) == 0

    @pytest.mark.asyncio
    async def test_stale_command_does_not_stop(self):
        """Command from previous turn does NOT stop current turn."""
        channel = InMemoryControlChannel()
        cmd = ControlCommand(
            command_id="stale", type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="s:main"),
            payload={"turn_uuid": "old-turn"},
        )
        await channel.send(cmd)
        ctx = _FakeContext(session_id="s:main", turn_uuid="current-turn")
        # Should NOT raise — stale command is consumed silently
        result = await drain_control_channel(channel, ctx, turn_uuid="current-turn")
        assert result is True  # consumed


class TestSessionIsolation:
    """Commands from one session never reach another session's consumer."""

    @pytest.mark.asyncio
    async def test_different_session_commands_isolated(self):
        channel = InMemoryControlChannel()
        # Session A sends /stop
        cmd_a = ControlCommand(
            command_id="iso-a", type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="A:main"),
        )
        await channel.send(cmd_a)

        # Session B drains — should find nothing
        ctx_b = _FakeContext(session_id="B:main", turn_uuid="t1")
        result = await drain_control_channel(channel, ctx_b, turn_uuid="t1")
        assert result is False

        # Session A's command still there
        remaining = await channel.peek(ControlScope(session_id="A:main"))
        assert len(remaining) == 1
