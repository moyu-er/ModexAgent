from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import (
    AgentDescriptor,
    AgentFactory,
    AgentLLMConfig,
    AgentPool,
    DefaultAgentFactory,
    SessionRetentionPolicy,
)
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.router import DefaultMeshRouter
from modex_agent.multi_agent.state import AgentState


@pytest.fixture
def any_broker():
    return InMemoryMessageBroker()


@pytest.fixture
def sample_descriptor():
    return AgentDescriptor(
        address=AgentAddress(name="test_agent", capabilities=["code"]),
        llm_config=AgentLLMConfig(model="gpt-4o"),
        system_prompt_template="You are a helpful assistant.",
        execution_strategy="react",
    )


# ── 4. Agent Factory ──


@pytest.mark.asyncio
async def test_default_agent_factory_pipeline_uses_mesh_router(sample_descriptor, any_broker):
    factory = DefaultAgentFactory(default_llm_provider=MagicMock())
    instance = await factory.create_agent(sample_descriptor, broker=any_broker)

    assert instance.pipeline is not None
    assert isinstance(instance.pipeline.router, DefaultMeshRouter)


def test_agent_factory_abc():
    class DummyFactory(AgentFactory):
        async def create_agent(self, descriptor, session_id=None, context_manager=None):
            return MagicMock()

    dummy = DummyFactory()
    assert dummy is not None


# ── 6. Agent Pool ──


@pytest.mark.asyncio
async def test_agent_pool_shutdown_all_stops_poller(any_broker):
    """shutdown_all stops the per-pool InboxPoller (Task 7).

    Replaces the old Drainer-cancellation test: the between-turn driver is now
    one InboxPoller per pool, owned by AgentPool and stopped from
    shutdown_all via stop_poller.
    """
    from modex_agent.multi_agent.inbox_poller import InboxPoller

    pool = AgentPool(broker=any_broker, agent_factory=MagicMock())

    # Build a poller with a live inflight turn task, then attach + start it.
    poller = InboxPoller(pool, interval=0.5)

    async def _long_running():
        await asyncio.sleep(100)

    poller._inflight["fake_session.main"] = asyncio.create_task(_long_running())
    pool.attach_poller(poller)
    pool.start_poller()

    fake_instance = MagicMock()
    fake_instance.stop = AsyncMock()
    pool._agents["fake"] = fake_instance
    pool._status["fake"] = AgentState.IDLE

    await pool.shutdown_all(timeout=0.1)
    assert pool.get_status("fake") == AgentState.SHUTDOWN
    # Poller loop task is cancelled/awaited by stop_poller.
    assert pool._poller._task is None or pool._poller._task.done()
    # Inflight turn task is cancelled by stop().
    assert "fake_session.main" not in pool._poller._inflight


@pytest.mark.asyncio
async def test_agent_pool_invalid_state_transition(any_broker):
    pool = AgentPool(broker=any_broker, agent_factory=MagicMock())
    pool._status["x"] = AgentState.SHUTDOWN
    pool._transition("x", AgentState.WORKING)
    assert pool.get_status("x") == AgentState.WORKING  # logged warning but applied


@pytest.mark.asyncio
async def test_agent_pool_session_cap_evicts_lru_after_touching_oldest(any_broker):
    fake_instance = MagicMock()
    fake_instance.context_manager = MagicMock()
    fake_instance.context_manager.clear = AsyncMock()
    fake_instance.stop = AsyncMock()

    pool = AgentPool(
        broker=any_broker,
        agent_factory=MagicMock(),
        retention=SessionRetentionPolicy(max_sessions_per_subagent=2),
    )
    mock_tree = MagicMock()
    mock_tree.on_session_evicted = AsyncMock()
    pool._tree = mock_tree
    pool._agents["worker"] = fake_instance
    try:
        pool._track_session("conv:worker:inv_old", "worker", is_dynamic=True)
        pool._track_session("conv:worker:inv_mid", "worker", is_dynamic=True)
        pool._touch_session("conv:worker:inv_old")
        pool._track_session("conv:worker:inv_new", "worker", is_dynamic=True)

        await pool._enforce_session_cap("worker")

        assert "conv:worker:inv_old" in pool._session_agents
        assert "conv:worker:inv_new" in pool._session_agents
        assert "conv:worker:inv_mid" not in pool._session_agents
        fake_instance.context_manager.clear.assert_awaited_once_with("conv:worker:inv_mid")
    finally:
        await pool.shutdown_all()


async def test_session_activity_records_created_at_and_last_active(any_broker):
    """created_at is immutable metadata; last_active refreshes on touch;
    _session_lru (int counter) bumps on track and touch."""
    pool = AgentPool(
        broker=any_broker,
        agent_factory=MagicMock(),
    )
    try:
        pool._track_session("conv:worker:inv", "worker", is_dynamic=True)
        activity = pool._session_activity["conv:worker:inv"]
        created0 = activity.created_at
        assert created0 == activity.last_active  # equal at creation

        lru0 = pool._session_lru["conv:worker:inv"]
        pool._touch_session("conv:worker:inv")

        activity = pool._session_activity["conv:worker:inv"]
        assert activity.created_at == created0  # immutable
        assert activity.last_active >= created0  # refreshed
        assert pool._session_lru["conv:worker:inv"] > lru0  # bumped
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_try_evict_if_stale_is_ttl_only_and_does_not_cap_evict(any_broker):
    """_try_evict_if_stale must NOT evict on count-cap (Policy 2 gone).
    Cap enforcement is _enforce_session_cap's sole responsibility.
    Over-cap but non-stale sessions survive _try_evict_if_stale."""
    fake_instance = MagicMock()
    fake_instance.context_manager = MagicMock()
    fake_instance.context_manager.clear = AsyncMock()
    fake_instance.stop = AsyncMock()

    pool = AgentPool(
        broker=any_broker,
        agent_factory=MagicMock(),
        retention=SessionRetentionPolicy(max_sessions_per_subagent=2, ttl_seconds=99999),
    )
    pool._agents["worker"] = fake_instance
    try:
        # 3 dynamic sessions for "worker" → over cap (2). None stale (huge ttl).
        pool._track_session("conv:worker:inv_a", "worker", is_dynamic=True)
        pool._track_session("conv:worker:inv_b", "worker", is_dynamic=True)
        pool._track_session("conv:worker:inv_c", "worker", is_dynamic=True)

        # inv_a is oldest by created_at — under old Policy 2 it would self-evict.
        await pool._try_evict_if_stale("conv:worker:inv_a")

        assert "conv:worker:inv_a" in pool._session_agents  # NOT evicted
        fake_instance.context_manager.clear.assert_not_awaited()
    finally:
        await pool.shutdown_all()
