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
