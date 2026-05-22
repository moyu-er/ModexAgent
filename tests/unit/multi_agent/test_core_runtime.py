from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.control.policy_registry import SupervisionPolicyRegistry, SupervisionPolicySpec
from framework.control.task_supervision import (
    NoOpSupervisionPolicy,
    SupervisionAction,
    SupervisionResult,
    TaskSupervisionPolicy,
    TaskSupervisor,
    TimeoutSupervisionPolicy,
)
from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentDescriptor,
    AgentDirectory,
    AgentFactory,
    AgentLLMConfig,
    AgentPool,
    AgentState,
    CommunicationTracker,
    CompositeTaskEventReporter,
    DefaultAgentFactory,
    InMemoryTaskCoordinator,
    InterruptibleRunner,
    LoggingTaskEventReporter,
    NullTaskCoordinator,
    ReActStrategy,
    SessionRetentionPolicy,
    SingleTurnStrategy,
    SubagentService,
    TaskCoordinator,
    TaskEvent,
    TaskEventBus,
    TaskEventReporter,
    TaskEventType,
    TaskProgressHook,
    TaskRecord,
)
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope


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
async def test_default_agent_factory_creates_session_agent(sample_descriptor):
    factory = DefaultAgentFactory()
    instance = await factory.create_agent(sample_descriptor, mode="session")
    assert instance.descriptor == sample_descriptor
    assert instance.session is not None


def test_agent_factory_abc():
    class DummyFactory(AgentFactory):
        async def create_agent(self, descriptor, mode, conversation_id=None, context_manager=None):
            return MagicMock()

    dummy = DummyFactory()
    assert dummy is not None


# ── 5. Registry / Directory ──

@pytest.mark.asyncio
async def test_agent_directory_register_and_find(sample_descriptor):
    directory = AgentDirectory()
    directory.register(sample_descriptor)
    assert directory.get_descriptor("test_agent") == sample_descriptor
    assert len(directory.list_agents()) == 1
    assert directory.get_status("test_agent") == AgentState.IDLE


def test_agent_directory_find_by_capability():
    directory = AgentDirectory()
    d1 = AgentDescriptor(address=AgentAddress(name="a1", capabilities=["code"]))
    d2 = AgentDescriptor(address=AgentAddress(name="a2", capabilities=["design"]))
    directory.register(d1)
    directory.register(d2)
    found = directory.find_by_capability("code")
    assert len(found) == 1
    assert found[0].address.name == "a1"


# ── 6. Agent Pool ──

@pytest.mark.asyncio
async def test_agent_pool_register_directory(any_broker):
    pool = AgentPool(broker=any_broker, agent_factory=MagicMock())
    descriptor = AgentDescriptor(address=AgentAddress(name="dir_agent"))
    await pool.register_directory(descriptor)
    assert pool.get_status("dir_agent") == AgentState.IDLE
    assert pool.get("dir_agent") is None


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


# ── 8. Task Coordinator ──

@pytest.mark.asyncio
async def test_in_memory_task_coordinator_register_and_query():
    coord = InMemoryTaskCoordinator()
    record = TaskRecord(task_id="t1", task_type="test", created_at=time.time(), conversation_id="c1")
    await coord.register_task("t1", record)
    fetched = await coord.get_task_record("t1")
    assert fetched is not None
    assert fetched.task_id == "t1"
    by_conv = await coord.get_task_records_by_conversation("c1")
    assert len(by_conv) == 1


@pytest.mark.asyncio
async def test_in_memory_task_coordinator_ttl_prune():
    coord = InMemoryTaskCoordinator(default_ttl_seconds=0.0)
    record = TaskRecord(
        task_id="t2",
        task_type="test",
        created_at=time.time() - 10,
        status="completed",
        updated_at=time.time() - 10,
    )
    await coord.register_task("t2", record)
    fetched = await coord.get_task_record("t2")
    assert fetched is None  # pruned


@pytest.mark.asyncio
async def test_in_memory_task_coordinator_replace_policy():
    coord = InMemoryTaskCoordinator()
    record = TaskRecord(task_id="t3", task_type="test", created_at=time.time())
    await coord.register_task("t3", record)
    p1 = TimeoutSupervisionPolicy(deadline=time.time() + 10)
    p2 = TimeoutSupervisionPolicy(deadline=time.time() + 20)
    await coord.bind_policy("t3", p1)
    await coord.bind_policy("t3", p2)
    rec = await coord.get_task_record("t3")
    assert len(rec.policies) == 1
    assert rec.policies[0].deadline == p2.deadline


@pytest.mark.asyncio
async def test_null_task_coordinator_noop():
    coord = NullTaskCoordinator()
    await coord.register_task("x", TaskRecord(task_id="x", task_type="t", created_at=0))
    assert await coord.get_task_record("x") is None
    assert await coord.get_task_records_by_conversation("c") == []
    assert await coord.get_task_records_by_status("s") == []


# ── 9. Intervention / Supervisor ──

@pytest.mark.asyncio
async def test_timeout_cancellation_policy():
    p = TimeoutSupervisionPolicy(deadline=time.time() - 1)
    rec = TaskRecord(task_id="t", task_type="t", created_at=0)
    result = await p.check(rec)
    assert result.action == SupervisionAction.CANCEL

    p2 = TimeoutSupervisionPolicy(deadline=time.time() + 100)
    result2 = await p2.check(rec)
    assert result2.action == SupervisionAction.PASS


@pytest.mark.asyncio
async def test_task_supervisor_cancels_main_task():
    coord = InMemoryTaskCoordinator()
    record = TaskRecord(task_id="task1", task_type="test", created_at=time.time())
    await coord.register_task("task1", record)
    await coord.bind_policy("task1", TimeoutSupervisionPolicy(deadline=time.time() - 0.1))

    supervisor = TaskSupervisor(coord, check_interval=0.05)

    async def _slow():
        await asyncio.sleep(10)
        return "done"

    coro = _slow()
    try:
        with pytest.raises(asyncio.CancelledError):
            await supervisor.supervise("task1", coro)
    finally:
        coro.close()


@pytest.mark.asyncio
async def test_task_supervisor_fault_tolerant_to_coordinator_failure():
    class BadCoordinator(TaskCoordinator):
        @property
        def event_bus(self):
            return None

        async def register_task(self, task_id, record):
            pass

        async def bind_policy(self, task_id, policy):
            raise RuntimeError("down")

        async def replace_policies(self, task_id, policies):
            pass

        async def get_task_record(self, task_id):
            raise RuntimeError("down")

        async def get_task_records_by_conversation(self, conversation_id):
            return []

        async def get_task_records_by_status(self, status):
            return []

        async def update_task_status(self, task_id, status, metadata=None):
            pass

        async def revoke_task(self, task_id):
            pass

    supervisor = TaskSupervisor(BadCoordinator(), check_interval=0.01)

    async def _quick():
        return "ok"

    result = await supervisor.supervise("x", _quick())
    assert result == "ok"


@pytest.mark.asyncio
async def test_no_op_intervention_policy():
    p = NoOpSupervisionPolicy()
    rec = TaskRecord(task_id="t", task_type="t", created_at=0)
    result = await p.check(rec)
    assert result.action == SupervisionAction.PASS


# ── 10. Event Bus / Reporters ──

@pytest.mark.asyncio
async def test_task_event_bus_emits():
    reporter = LoggingTaskEventReporter()
    bus = TaskEventBus(reporter)
    event = TaskEvent(task_id="t1", event_type=TaskEventType.STARTED)
    await bus.emit(event)  # should not raise


@pytest.mark.asyncio
async def test_composite_reporter_isolates_failures():
    class BadReporter(TaskEventReporter):
        async def report(self, event):
            raise RuntimeError("boom")

    bad = BadReporter()
    good = LoggingTaskEventReporter()
    composite = CompositeTaskEventReporter([bad, good])
    event = TaskEvent(task_id="t1", event_type=TaskEventType.STARTED)
    await composite.report(event)  # should not raise despite bad reporter


@pytest.mark.asyncio
async def test_in_memory_coordinator_event_bus_isolation():
    class BadReporter(TaskEventReporter):
        async def report(self, event):
            raise RuntimeError("boom")

    bad_reporter = BadReporter()
    bus = TaskEventBus(bad_reporter)
    coord = InMemoryTaskCoordinator(event_bus=bus)
    rec = TaskRecord(task_id="t", task_type="t", created_at=time.time())
    await coord.register_task("t", rec)  # should not raise


# ── 11. Policy Registry ──

class DummyPolicy(TaskSupervisionPolicy):
    policy_type = "dummy"

    async def check(self, task_record):
        return SupervisionResult()

    @classmethod
    def from_config(cls, config):
        return cls()


def test_policy_registry_register_and_get():
    SupervisionPolicyRegistry.register("dummy", DummyPolicy)
    assert SupervisionPolicyRegistry.get("dummy") is DummyPolicy


def test_policy_spec_round_trip():
    SupervisionPolicyRegistry.register("dummy2", DummyPolicy)
    spec = SupervisionPolicySpec(policy_type="dummy2", config={})
    policy = spec.to_policy()
    assert isinstance(policy, DummyPolicy)


# ── 12. Hooks / Strategies / InterruptibleRunner ──

@pytest.mark.asyncio
async def test_interruptible_runner_returns_cancel_result():
    runner = InterruptibleRunner()

    class FakeAgent:
        async def run(self, ctx, emitter):
            raise asyncio.CancelledError()

    class FakeEmitter:
        def get_content(self):
            return "partial"

    ctx = MagicMock()
    ctx.history.to_list = AsyncMock(return_value=[])
    result = await runner.run(FakeAgent(), ctx, FakeEmitter())
    assert result.stop_reason == "cancelled"
    assert result.partial_content == "partial"


@pytest.mark.asyncio
async def test_react_strategy_delegates_to_agent():
    strategy = ReActStrategy()
    agent = MagicMock()
    agent.run = AsyncMock(return_value=AgentResult(content="ok"))
    ctx = MagicMock()
    emitter = MagicMock()
    result = await strategy.execute(agent, ctx, emitter)
    assert result.content == "ok"
    agent.run.assert_awaited_once()


def test_single_turn_strategy_requires_llm_provider():
    strategy = SingleTurnStrategy()
    agent = MagicMock()
    agent.provider = None
    with pytest.raises(RuntimeError):
        asyncio.run(strategy.execute(agent, MagicMock(), MagicMock()))


@pytest.mark.asyncio
async def test_control_drain_interceptor_raises_on_cancel():
    """ControlDrainInterceptor 替代 TaskInterventionHook 的功能。

    在 iteration 边界消费 cancel 命令并抛出 AgentCancelled。
    """
    from framework.control.channel import InMemoryControlChannel
    from framework.control.exceptions import AgentCancelled
    from framework.control.types import ControlCommand, ControlCommandType, ControlScope
    from framework.interceptor.abc import IterationContext
    from framework.interceptor.builtin.control_drain import ControlDrainInterceptor

    channel = InMemoryControlChannel()
    interceptor = ControlDrainInterceptor(channel=channel)
    await channel.send(
        ControlCommand(
            command_id="cmd-1",
            type=ControlCommandType.CANCEL_TURN,
            scope=ControlScope(session_id="s1"),
        )
    )

    ctx = MagicMock()
    ctx.session_id = "s1"
    ctx.runtime = None

    async def next_call() -> None:
        pass

    with pytest.raises(AgentCancelled):
        await interceptor.around_iteration(ctx, IterationContext(iteration=1, turn_id="t1"), next_call)


@pytest.mark.asyncio
async def test_task_progress_hook_reports():
    reporter = MagicMock()
    reporter.report = AsyncMock()
    from framework.multi_agent.event_bus import TaskEventBus

    bus = TaskEventBus(reporter)
    hook = TaskProgressHook("t1", bus)
    ctx = MagicMock()
    ctx.session_id = "s1"
    await hook.before_iteration(ctx)
    await hook.before_tool_execution(ctx, [MagicMock()])
    state = hook._state["s1"]
    assert state["iteration"] == 1
    assert state["tool_calls"] == 1
    reporter.report.assert_awaited()


# ── 13. Subagent Manager ──

@pytest.mark.asyncio
async def test_subagent_service_spawn_and_wait(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.process_message = AsyncMock(return_value=AgentResult(content="result"))
    fake_instance.stop = AsyncMock()
    fake_instance.tool_manager = InMemoryToolManager()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    pool = AgentPool(
        broker=any_broker,
        agent_factory=factory,
        enable_inbox_polling=False,
    )
    mgr = SubagentService(
        pool=pool,
        factory=factory,
        broker=any_broker,
        agent_bus=MagicMock(),
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="sub1"))
    try:
        result = await mgr.create_and_wait(
            descriptor=descriptor,
            task_prompt="do work",
            timeout=1.0,
        )
    finally:
        await pool.shutdown_all()
    assert result.content == "result"


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
async def test_subagent_service_create_and_wait_timeout_clears_future(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.process_message = AsyncMock(side_effect=asyncio.TimeoutError)
    fake_instance.stop = AsyncMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    pool = AgentPool(
        broker=any_broker,
        agent_factory=factory,
        enable_inbox_polling=False,
    )
    mgr = SubagentService(
        pool=pool,
        factory=factory,
        broker=any_broker,
        agent_bus=MagicMock(),
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="sub3"))
    try:
        result = await mgr.create_and_wait(
            descriptor=descriptor,
            task_prompt="hi",
            timeout=0.01,
        )
    finally:
        await pool.shutdown_all()
    assert result.stop_reason == "timeout"
    assert pool._sync_futures == {}


@pytest.mark.asyncio
async def test_subagent_service_create_and_wait_forwards_factory_params(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.pipeline.process_message = AsyncMock(return_value=AgentResult(content="ok"))
    fake_instance.stop = AsyncMock()
    fake_instance.tool_manager = InMemoryToolManager()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    pool = AgentPool(
        broker=any_broker,
        agent_factory=factory,
        enable_inbox_polling=False,
    )
    mgr = SubagentService(
        pool=pool,
        factory=factory,
        broker=any_broker,
        agent_bus=MagicMock(),
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="sub_params"))
    try:
        result = await mgr.create_and_wait(
            descriptor=descriptor,
            task_prompt="do work",
            timeout=1.0,
        )
    finally:
        await pool.shutdown_all()
    assert result.content == "ok"
    call_args = factory.create_agent.await_args
    assert call_args.args[0] == descriptor
    call_kwargs = call_args.kwargs
    assert call_kwargs["mode"] == "pipeline"


@pytest.mark.asyncio
async def test_subagent_service_registers_resident(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    pool = MagicMock()
    pool.register_resident = AsyncMock(return_value=fake_instance)
    mgr = SubagentService(
        pool=pool,
        factory=factory,
        broker=any_broker,
        agent_bus=MagicMock(),
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="sub4"))
    result = await mgr.register_resident(
        descriptor=descriptor,
        context_manager=MagicMock(),
    )
    assert result is fake_instance
    pool.register_resident.assert_awaited_once()


@pytest.mark.asyncio
async def test_subagent_service_admit_dynamic_namespaces_descriptor(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    fake_instance.pipeline = MagicMock()
    fake_instance.stop = AsyncMock()
    fake_instance.context_manager = MagicMock()
    factory.create_agent = AsyncMock(return_value=fake_instance)
    pool = AgentPool(
        broker=any_broker,
        agent_factory=factory,
        enable_inbox_polling=False,
    )
    mgr = SubagentService(
        pool=pool,
        factory=factory,
        broker=any_broker,
        agent_bus=MagicMock(),
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="worker"))
    try:
        session_id = await mgr.admit_dynamic(
            descriptor=descriptor,
            initial_task="do work",
        )
        dynamic_names = [name for name in pool._agents if name.startswith("dyn.worker.")]
        assert len(dynamic_names) == 1
        assert "worker" not in pool._agents
        assert dynamic_names[0] in session_id
    finally:
        await pool.shutdown_all()


@pytest.mark.asyncio
async def test_default_agent_factory_uses_allowed_skills():
    from framework.core.skills import FileSkillSource, ProgressiveBuilder, SkillManager

    factory = DefaultAgentFactory()
    skills_dir = Path(__file__).parent / "skills"
    skills_dir.mkdir(exist_ok=True)
    source = FileSkillSource(directories=[skills_dir], cache=False)
    base_skill_mgr = SkillManager(source=source, builder=ProgressiveBuilder())
    factory._skill_manager = base_skill_mgr

    descriptor = AgentDescriptor(
        address=AgentAddress(name="test"),
        allowed_skills=["helper"],
    )
    instance = await factory.create_agent(descriptor, mode="session")
    assert instance.session is not None
    # skill_manager on session should be an AgentSkillManager wrapper
    from framework.core.skills.filter import SkillWhitelistFilter as AgentSkillManager
    assert isinstance(instance.session._skill_manager, AgentSkillManager)


@pytest.mark.asyncio
async def test_default_agent_factory_uses_passed_tool_manager():
    custom_tm = InMemoryToolManager()
    factory = DefaultAgentFactory()
    descriptor = AgentDescriptor(
        address=AgentAddress(name="test"),
    )
    instance = await factory.create_agent(descriptor, mode="session", tool_manager=custom_tm)
    assert instance.tool_manager is not None
    # When tool_manager is passed as argument, factory should use it
    assert instance.tool_manager._base is custom_tm


@pytest.mark.asyncio
async def test_default_agent_factory_ephemeral_context_manager():
    from framework.core.context import EphemeralContextManager

    factory = DefaultAgentFactory()
    descriptor = AgentDescriptor(
        address=AgentAddress(name="test"),
        context_strategy="ephemeral",
    )
    instance = await factory.create_agent(descriptor, mode="session")
    assert isinstance(instance.context_manager, EphemeralContextManager)


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
            sid for sid, meta in pool._session_meta.items()
            if meta.agent_name == "worker"
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

        assert "conv:worker:inv_old" in pool._session_meta
        assert "conv:worker:inv_new" in pool._session_meta
        assert "conv:worker:inv_mid" not in pool._session_meta
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
