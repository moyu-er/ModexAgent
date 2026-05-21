"""Tests for AgentPool dispatch behavior."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.graph.interrupt import GraphInterrupt
from framework.multi_agent.pool import AgentPool
from framework.multi_agent.state import AgentState


class _FakeBroker:
    async def consume(self, address):
        return None

    async def send_to(self, address, msg):
        pass


class TestRunDispatch:
    """AgentPool._run_dispatch must propagate GraphInterrupt, not swallow it."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_run_dispatch_propagates_graph_interrupt(self, pool):
        """Regression: GraphInterrupt raised by the coroutine must propagate
        upward so the pipeline's approval handler can catch it.

        Before fix: caught by bare ``except Exception`` → logged as error
        and the agent state transitioned to ERROR.
        After fix: re-raised unchanged.
        """
        async def _raising_coro():
            raise GraphInterrupt(value=["test"])

        with pytest.raises(GraphInterrupt):
            await pool._run_dispatch("main", _raising_coro())

        # Agent should stay IDLE, not transition to ERROR
        assert pool.get_status("main") != AgentState.ERROR

    @pytest.mark.asyncio
    async def test_run_dispatch_does_not_transition_to_error_on_interrupt(self, pool):
        """If GraphInterrupt is swallowed, the agent transitions to ERROR.
        After the fix it must remain IDLE (or the state it had before)."""
        pool._status["main"] = AgentState.IDLE

        async def _raising_coro():
            raise GraphInterrupt(value=["test"])

        with pytest.raises(GraphInterrupt):
            await pool._run_dispatch("main", _raising_coro())

        assert pool._status.get("main") == AgentState.IDLE


class TestDispatchTaskRequestFallback:
    """_dispatch_task_request must accept legacy envelopes with ``content`` as
    a defensive fallback for ``task_prompt``."""

    @pytest.fixture
    async def pool(self):
        p = AgentPool(
            broker=_FakeBroker(),
            agent_factory=MagicMock(),
            enable_inbox_polling=False,
        )
        yield p
        await p.shutdown_all(timeout=0.1)

    @pytest.mark.asyncio
    async def test_dispatch_falls_back_to_content_when_task_prompt_missing(self, pool):
        """When the envelope payload has ``content`` but no ``task_prompt``,
        _dispatch_task_request should still extract the task via the fallback."""
        from framework.core.context import InMemoryContextManager
        from framework.core.types import InputMessage
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.descriptor import AgentDescriptor, AgentInstance
        from framework.multi_agent.envelope import AgentMessageEnvelope
        from framework.pipeline.pipeline import AgentPipeline

        from framework.core.agent import Agent
        from framework.core.tool_manager import InMemoryToolManager

        desc = AgentDescriptor(address=AgentAddress(name="worker"))
        agent_stub = MagicMock(spec=Agent)
        agent_stub.name = "worker"

        pipeline_stub = MagicMock(spec=AgentPipeline)
        processed_content = []

        async def _fake_process(msg):
            processed_content.append(msg.content)
            from framework.core.emitter import AgentResult
            return AgentResult(content="done")

        pipeline_stub.process_message.side_effect = _fake_process
        instance = AgentInstance(
            descriptor=desc,
            agent=agent_stub,
            tool_manager=InMemoryToolManager(),
            pipeline=pipeline_stub,
            context_manager=InMemoryContextManager(),
        )

        envelope = AgentMessageEnvelope(
            payload={"content": "legacy task", "message_type": "task_request"},
            source=AgentAddress(name="main"),
            message_type="task_request",
            conversation_id="conv",
        )

        await pool._dispatch_task_request(instance, desc, envelope)
        assert processed_content, "Pipeline should have been called"
        assert processed_content[0] == "legacy task", (
            f"Expected 'legacy task' but got {processed_content[0]!r}"
        )
