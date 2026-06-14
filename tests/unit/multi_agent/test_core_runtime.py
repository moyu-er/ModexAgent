from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.emitter import AgentResult
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentDescriptor,
    AgentFactory,
    AgentLLMConfig,
    AgentPool,
    AgentState,
    CommunicationTracker,
    DefaultAgentFactory,
    SessionRetentionPolicy,
)
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.router import DefaultMeshRouter


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
        async def create_agent(self, descriptor, conversation_id=None, context_manager=None):
            return MagicMock()

    dummy = DummyFactory()
    assert dummy is not None


# ── 6. Agent Pool ──

@pytest.mark.asyncio
async def test_agent_pool_shutdown_all_cancels_consumers(any_broker):
    pool = AgentPool(broker=any_broker, agent_factory=MagicMock())
    # Create a fake consumer task
    async def _long_running():
        await asyncio.sleep(100)

    pool._consumers["fake"] = asyncio.create_task(_long_running())
    fake_instance = MagicMock()
    fake_instance.stop = AsyncMock()
    pool._agents["fake"] = fake_instance
    pool._status["fake"] = AgentState.IDLE

    await pool.shutdown_all(timeout=0.1)
    assert pool.get_status("fake") == AgentState.SHUTDOWN
    assert "fake" not in pool._consumers


@pytest.mark.asyncio
async def test_agent_pool_invalid_state_transition(any_broker):
    pool = AgentPool(broker=any_broker, agent_factory=MagicMock())
    pool._status["x"] = AgentState.SHUTDOWN
    pool._transition("x", AgentState.WORKING)
    assert pool.get_status("x") == AgentState.WORKING  # logged warning but applied


@pytest.mark.asyncio
async def test_agent_pool_get_lock(any_broker):
    pool = AgentPool(broker=any_broker, agent_factory=MagicMock())
    lock1 = pool.get_lock("sid_1")
    lock2 = pool.get_lock("sid_1")
    assert lock1 is lock2


def test_communication_tracker_acknowledges_owner_digest():
    tracker = CommunicationTracker()
    tracker.record_send(
        agent_name="main",
        target_agent="worker",
        invocation_id="inv_1",
        session_id="conv:worker:inv_1",
        content_summary="review file",
    )

    assert len(tracker.get_pending_for_agent("main")) == 1

    record = tracker.acknowledge(
        invocation_id="inv_1",
        reply_from="worker",
        reply_summary="done",
    )

    assert record is not None
    assert tracker.get_pending_for_agent("main") == []
    digest = tracker.get_digest_for_agent("main")
    assert digest.acknowledged == [record]


def test_communication_tracker_reply_closes_received_bracket():
    tracker = CommunicationTracker()
    tracker.record_receive(
        agent_name="worker",
        source_agent="main",
        invocation_id="inv_1",
        content_summary="review file",
    )

    assert len(tracker.get_pending_for_agent("worker")) == 1

    record = tracker.record_send(
        agent_name="worker",
        target_agent="main",
        invocation_id="inv_1",
        session_id="conv:worker:inv_1",
        content_summary="done",
    )

    assert tracker.get_pending_for_agent("worker") == []
    digest = tracker.get_digest_for_agent("worker")
    assert digest.acknowledged == [record]


@pytest.mark.asyncio
async def test_agent_pool_resets_error_count_on_success(any_broker):
    factory = MagicMock()
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.process_message = AsyncMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    descriptor = AgentDescriptor(
        address=AgentAddress(name="resilient_agent", capabilities=["code"]),
        context_strategy="persistent",
    )
    pool = AgentPool(broker=any_broker, agent_factory=factory)
    await pool.register_resident(descriptor)

    # Pre-set error count
    pool._error_counts["resilient_agent"] = 3

    # Mock consume to return one valid message then cancel
    msg = MagicMock()
    msg.headers = {}
    msg.payload = {"content": "hi", "conversation_id": "c1", "agent_session_id": "c1:resilient_agent"}

    original_consume = any_broker.consume
    call_count = 0

    async def _mock_consume(address):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return msg
        raise asyncio.CancelledError()

    any_broker.consume = _mock_consume
    try:
        await pool._consume_messages(fake_instance, descriptor)
    finally:
        any_broker.consume = original_consume

    # 等待后台 _run_dispatch 任务完成
    tasks = pool._agent_tasks.get("resilient_agent", [])
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    assert "resilient_agent" not in pool._error_counts


@pytest.mark.asyncio
async def test_agent_pool_tracks_and_caps_invocation_sessions(any_broker):
    """Pool dispatch should track invocation sessions and keep only the latest cap."""
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.process_message = AsyncMock(return_value=AgentResult(content="ok"))
    fake_instance.context_manager = MagicMock()
    fake_instance.context_manager.clear = AsyncMock()
    fake_instance.stop = AsyncMock()
    descriptor = AgentDescriptor(address=AgentAddress(name="worker"))
    fake_instance.descriptor = descriptor

    pool = AgentPool(
        broker=any_broker,
        agent_factory=MagicMock(),
        enable_inbox_polling=False,
        retention=SessionRetentionPolicy(max_sessions_per_subagent=10),
    )
    pool._agents["worker"] = fake_instance

    try:
        for index in range(11):
            invocation_id = f"inv_{index:02d}"
            envelope = AgentMessageEnvelope(
                payload={
                    "content": f"task {index}",
                    "task_prompt": f"task {index}",
                    "message_type": "task_request",
                    "invocation_id": invocation_id,
                },
                source=AgentAddress(name="main"),
                target=AgentAddress(name="worker"),
                message_type="task_request",
                conversation_id="conv",
                agent_session_id=f"conv:worker:{invocation_id}",
                correlation_id=invocation_id,
            )
            await pool._dispatch_task_request(fake_instance, descriptor, envelope)

        worker_sessions = [
            sid for sid, agent in pool._session_agents.items()
            if agent == "worker"
        ]
        assert len(worker_sessions) == 10
        assert "conv:worker:inv_00" not in worker_sessions
        assert "conv:worker:inv_10" in worker_sessions
        fake_instance.context_manager.clear.assert_any_await("conv:worker:inv_00")
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_agent_pool_session_cap_evicts_lru_after_touching_oldest(any_broker):
    fake_instance = MagicMock()
    fake_instance.context_manager = MagicMock()
    fake_instance.context_manager.clear = AsyncMock()
    fake_instance.stop = AsyncMock()

    pool = AgentPool(
        broker=any_broker,
        agent_factory=MagicMock(),
        enable_inbox_polling=False,
        retention=SessionRetentionPolicy(max_sessions_per_subagent=2),
    )
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


@pytest.mark.asyncio
async def test_agent_pool_injects_communication_sideband_metadata(any_broker):
    tracker = CommunicationTracker()
    tracker.record_receive(
        agent_name="worker",
        source_agent="main",
        invocation_id="inv_1",
        content_summary="review file",
    )
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.process_message = AsyncMock(return_value=AgentResult(content="ok"))
    fake_instance.stop = AsyncMock()
    descriptor = AgentDescriptor(address=AgentAddress(name="worker"))
    fake_instance.descriptor = descriptor
    pool = AgentPool(
        broker=any_broker,
        agent_factory=MagicMock(),
        enable_inbox_polling=False,
        comm_tracker=tracker,
    )
    pool._agents["worker"] = fake_instance
    try:
        envelope = AgentMessageEnvelope(
            payload={
                "content": "follow up",
                "message_type": "agent_message",
            },
            source=AgentAddress(name="main"),
            target=AgentAddress(name="worker"),
            message_type="agent_message",
            conversation_id="conv",
            agent_session_id="conv:worker",
        )
        await pool._dispatch_agent_message(fake_instance, envelope)

        input_msg = fake_instance.pipeline.process_message.await_args.args[0]
        sideband = input_msg.metadata["sideband_system_prompt"]
        assert "Pending Communications" in sideband
        assert "inv_1" in sideband
    finally:
        await pool.shutdown_all()
