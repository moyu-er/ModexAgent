"""Tests for AgentPipeline session resource cleanup (P0-2r2)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.pipeline.pipeline import AgentPipeline


class _MinimalAgent:
    name = "test-agent"

    async def run(self, context, emitter):
        from framework.core.emitter import AgentResult
        return AgentResult(content="ok")


class _MinimalToolManager:
    async def startup(self):
        pass

    async def shutdown(self):
        pass

    async def execute(self, *args, **kwargs):
        from framework.core.tool_manager import ToolResult
        return ToolResult(tool_name="test", result="ok")

    def get_tool_descriptions(self, caller_context=None):
        return []


class _MinimalInputAdapter:
    def __init__(self):
        self._sent = False

    async def start(self):
        pass

    async def stop(self):
        pass

    async def receive(self):
        if not self._sent:
            self._sent = True
            from framework.core.session_id import SessionId
            from framework.core.types import InputMessage
            yield InputMessage(content="test", session=SessionId.from_str("s1", default_agent_name="main"))


class _MinimalOutputAdapter:
    async def send(self, message, session_id):
        pass


class TestPipelineCleanupSessionResources:
    """P0-2r2: cleanup_session_resources must be called when session ends."""

    async def test_cleanup_session_resources_clears_per_session_state(self):
        """Direct call to cleanup_session_resources clears session state."""
        channel = MagicMock()
        channel.cleanup_session = AsyncMock()

        pipeline = AgentPipeline(
            agent=_MinimalAgent(),
            context_manager=MagicMock(),
            tool_manager=_MinimalToolManager(),
            input_adapter=_MinimalInputAdapter(),
            output_adapter=_MinimalOutputAdapter(),
            control_channel=channel,
        )

        sid = "s1"
        pipeline._session_locks[sid] = "fake_lock"
        pipeline._injection_queues[sid] = "fake_queue"
        pipeline._session_tasks[sid] = "fake_task"

        await pipeline.cleanup_session_resources(sid)

        assert sid not in pipeline._session_locks
        assert sid not in pipeline._injection_queues
        assert sid not in pipeline._session_tasks
        channel.cleanup_session.assert_awaited_once_with(sid)

    async def test_stop_cleans_up_session_resources(self):
        """Pipeline.stop() calls cleanup_session_resources for lingering sessions."""
        channel = MagicMock()
        channel.cleanup_session = AsyncMock()

        pipeline = AgentPipeline(
            agent=_MinimalAgent(),
            context_manager=MagicMock(),
            tool_manager=_MinimalToolManager(),
            input_adapter=_MinimalInputAdapter(),
            output_adapter=_MinimalOutputAdapter(),
            control_channel=channel,
        )

        # Simulate lingering session resources that were not cleaned up
        pipeline._session_locks["s1"] = "fake_lock"
        pipeline._injection_queues["s1"] = "fake_queue"

        await pipeline.stop()

        # After stop(), session resources should be cleaned up
        assert len(pipeline._session_locks) == 0, (
            f"stop() should clean _session_locks, got {pipeline._session_locks}"
        )
        assert len(pipeline._injection_queues) == 0, (
            f"stop() should clean _injection_queues, got {pipeline._injection_queues}"
        )
        assert channel.cleanup_session.called, (
            "stop() should call control_channel.cleanup_session"
        )
