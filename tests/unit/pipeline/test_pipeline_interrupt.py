"""Tests for AgentPipeline exception propagation (defense in depth)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.core.graph.interrupt import GraphInterrupt
from framework.core.types import InputMessage
from framework.pipeline.pipeline import AgentPipeline


class _FakeInputAdapter:
    """Async generator that yields one message then stops."""

    def __init__(self, messages: list[InputMessage]):
        self._messages = messages
        self._started = False
        self._stopped = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    async def receive(self):
        for msg in self._messages:
            yield msg


class TestPipelineRunInterrupt:
    """AgentPipeline.run() must propagate GraphInterrupt, not swallow it."""

    @pytest.fixture
    def pipeline(self):
        tool_manager = MagicMock()
        tool_manager.startup = AsyncMock()
        tool_manager.shutdown = AsyncMock()
        output_adapter = MagicMock()
        output_adapter.send = AsyncMock()
        p = AgentPipeline(
            agent=MagicMock(),
            context_manager=MagicMock(),
            tool_manager=tool_manager,
            input_adapter=MagicMock(),
            output_adapter=output_adapter,
        )
        return p

    @pytest.mark.asyncio
    async def test_run_propagates_graph_interrupt(self, pipeline):
        """Regression: if GraphInterrupt escapes _process_message, run()
        must propagate it rather than swallowing as a generic error.

        Before fix: caught by ``except Exception`` → logged and loop continued.
        After fix: re-raised unchanged.
        """
        msg = InputMessage(content="trigger approval", session_id="s1")
        pipeline.input_adapter = _FakeInputAdapter([msg])

        with patch.object(
            pipeline, "_process_message", side_effect=GraphInterrupt(value=["test"])
        ):
            with pytest.raises(GraphInterrupt):
                await pipeline.run()

    @pytest.mark.asyncio
    async def test_run_handles_regular_exception(self, pipeline):
        """Regular exceptions should still be caught and logged."""
        msg = InputMessage(content="trigger error", session_id="s1")
        pipeline.input_adapter = _FakeInputAdapter([msg])

        with patch.object(
            pipeline, "_process_message", side_effect=RuntimeError("boom")
        ):
            # Should NOT raise — regular exceptions are swallowed and logged
            await pipeline.run()
