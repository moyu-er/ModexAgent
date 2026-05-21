from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.emitter import AgentResult
from framework.core.tool_manager import InMemoryToolManager
from framework.messaging.broker import Address
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.control.task_supervision import (
    SupervisionAction,
    SupervisionResult,
    NoOpSupervisionPolicy,
    TaskSupervisionPolicy,
    TaskSupervisor,
    TimeoutSupervisionPolicy,
)
from framework.control.policy_registry import SupervisionPolicyRegistry, SupervisionPolicySpec
from framework.multi_agent import (
    AgentDescriptor,
    AgentDirectory,
    AgentFactory,
    AgentLLMConfig,
    AgentPool,
    AgentState,
    CompositeTaskEventReporter,
    DefaultAgentFactory,
    InMemoryTaskCoordinator,
    InterruptibleRunner,
    LoggingTaskEventReporter,
    NullTaskCoordinator,
    ReActStrategy,
    SingleTurnStrategy,
    SubagentService,
    SessionRetentionPolicy,
    TaskCoordinator,
    TaskEvent,
    TaskEventBus,
    TaskEventReporter,
    TaskEventType,
    TaskProgressHook,
    TaskRecord,
)
from framework.multi_agent.address import AgentAddress


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
    fake_instance.session.process_message = AsyncMock(return_value=AgentResult(content="result"))
    fake_instance.tool_manager = InMemoryToolManager()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    mgr = SubagentService(broker=any_broker, agent_factory=factory, coordination_config=SessionRetentionPolicy(enable_for_subagent=False))
    descriptor = AgentDescriptor(address=AgentAddress(name="sub1"))
    result = await mgr.spawn_and_wait(
        parent_address=AgentAddress(name="main"),
        descriptor=descriptor,
        task_prompt="do work",
        conversation_id="c1",
        timeout=1.0,
    )
    assert result.content == "result"


@pytest.mark.asyncio
async def test_subagent_service_uses_null_coordinator_when_disabled(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    fake_instance.session.process_message = AsyncMock(return_value=AgentResult(content="ok"))
    fake_instance.tool_manager = InMemoryToolManager()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    mgr = SubagentManager(
        broker=any_broker,
        agent_factory=factory,
        task_coordinator=None,
        coordination_config=SessionRetentionPolicy(enable_for_subagent=False),
    )
    assert isinstance(mgr._coordinator, NullTaskCoordinator)
    descriptor = AgentDescriptor(address=AgentAddress(name="sub3"))
    result = await mgr.spawn_and_wait(
        parent_address=AgentAddress(name="main"),
        descriptor=descriptor,
        task_prompt="hi",
        conversation_id="c3",
    )
    assert result.content == "ok"


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
async def test_subagent_service_forwards_session_params(any_broker):
    factory = MagicMock(spec=AgentFactory)
    fake_instance = MagicMock()
    fake_instance.session.process_message = AsyncMock(return_value=AgentResult(content="ok"))
    fake_instance.tool_manager = InMemoryToolManager()
    factory.create_agent = AsyncMock(return_value=fake_instance)

    sanitizer = lambda x: x.strip()
    interceptor = MagicMock()
    mgr = SubagentManager(
        broker=any_broker,
        agent_factory=factory,
        coordination_config=SessionRetentionPolicy(enable_for_subagent=False),
        sanitizer=sanitizer,
        command_interceptor=interceptor,
    )
    descriptor = AgentDescriptor(address=AgentAddress(name="sub_params"))
    result = await mgr.spawn_and_wait(
        parent_address=AgentAddress(name="main"),
        descriptor=descriptor,
        task_prompt="do work",
        conversation_id="c1",
        timeout=1.0,
    )
    assert result.content == "ok"
    call_kwargs = factory.create_agent.await_args.kwargs
    assert call_kwargs.get("sanitizer") is sanitizer
    assert call_kwargs.get("command_interceptor") is interceptor
    assert call_kwargs.get("subagent_service") is mgr


