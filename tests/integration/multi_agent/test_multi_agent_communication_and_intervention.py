"""Integration tests for multi-agent communication, context injection,
independent configuration, intervention, and feedback/reporting.

验证场景（对照 docs/multi_agent_system_design.md）：
1. Agent 之间通过 Broker + Envelope 通信
2. Agent 消息通过 InboxFlushHook 注入上下文
3. 多 Agent 独立 Tool / Skill 配置
4. 实时/初始干预能力（TaskSupervisor, TaskInterventionHook, 手动 cancel）
5. 反馈与上报能力（超时中断、手动终止、事件上报平台中心）
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.integration

from framework.agents.react import ReActAgent, ReActEvent
from framework.core.agent import AgentContext
from framework.core.context import InMemoryContextManager
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import AgentResult, BufferingEmitter
from framework.core.provider import StreamingLLMProvider
from framework.core.tool_manager import InMemoryToolManager, Tool, ToolConfig, ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.messaging.broker_memory import InMemoryMessageBroker
from framework.multi_agent import (
    AgentDescriptor,
    AgentLLMConfig,
    DefaultAgentFactory,
    InMemoryTaskCoordinator,
    NullTaskCoordinator,
    SubagentManager,
    TaskCoordinationConfig,
    TaskEvent,
    TaskEventBus,
    TaskEventType,
    TaskInterventionHook,
    TaskProgressHook,
    TaskRecord,
    TaskSupervisor,
    TimeoutCancellationPolicy,
)
from framework.hook.builtin import InboxFlushHook
from framework.multi_agent.inbox.producer import InboxProducer
from framework.multi_agent.inbox.consumer import InboxConsumer
from framework.multi_agent.inbox.server_memory import InMemoryInboxServer
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope
from framework.multi_agent.event_bus import (
    CompositeTaskEventReporter,
    TaskEventReporter,
)
from framework.multi_agent.filtered_tool_manager import FilteredToolManager
from framework.core.runner import InterruptibleRunner
from framework.multi_agent.pool import AgentPool
from framework.multi_agent.tools import SendMessageTool


# ── Fixtures ──

@pytest.fixture
def broker():
    return InMemoryMessageBroker()


@pytest.fixture
def factory():
    return DefaultAgentFactory()


class MockStreamingProvider(StreamingLLMProvider):
    """可编程的 Mock LLM Provider，用于 ReActAgent 集成测试。"""

    def __init__(self):
        self._responses: list[LLMResponse] = []
        self._response_index = 0

    def set_responses(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._response_index = 0

    async def chat(self, messages, **kwargs):
        resp = self._responses[self._response_index]
        self._response_index = (self._response_index + 1) % len(self._responses)
        return resp

    async def chat_stream(self, messages, on_content_delta=None, on_reasoning_delta=None, **kwargs):
        resp = self._responses[self._response_index]
        self._response_index = (self._response_index + 1) % len(self._responses)
        if resp.content and on_content_delta:
            for ch in resp.content:
                await on_content_delta(ch)
        if resp.reasoning_content and on_reasoning_delta:
            for ch in resp.reasoning_content:
                await on_reasoning_delta(ch)
        return resp

    def get_default_model(self):
        return "mock-model"


@pytest.fixture
def mock_provider():
    return MockStreamingProvider()


@pytest.fixture
def memory_event_reporter():
    """录制所有收到的事件，用于验证上报链路。"""

    class MemoryReporter(TaskEventReporter):
        def __init__(self):
            self.events: list[TaskEvent] = []

        async def report(self, event: TaskEvent) -> None:
            self.events.append(event)

    return MemoryReporter()


# ── 1. Agent 间通信（SendMessageTool + Broker + Envelope） ──

@pytest.mark.asyncio
async def test_send_message_tool_routes_via_envelope(broker):
    """SendMessageTool 应构造 AgentMessageEnvelope 并通过 Broker 路由到目标 Agent。"""
    parent = AgentAddress(name="main")
    tool = SendMessageTool(broker=broker, self_address=parent, allowed_callers=None)

    result = await tool.execute(
        target_agent="helper",
        content="hello helper",
        conversation_id="c1",
        agent_session_id="c1:helper",
    )
    assert "helper" in result

    # 从 helper 的 mailbox 取出消息
    msg = await broker.consume(AgentAddress(kind="agent", name="helper"))
    envelope = AgentMessageEnvelope.from_broker_message(msg)
    assert envelope is not None
    assert envelope.payload.get("content") == "hello helper"
    assert envelope.source.name == "main"
    assert envelope.target.name == "helper"
    assert envelope.conversation_id == "c1"
    assert envelope.agent_session_id == "c1:helper"


@pytest.mark.asyncio
async def test_agent_pool_consumes_envelope_and_injects_to_pipeline(broker, factory):
    """AgentPool 常驻 Agent 应能通过 Broker 消费 Envelope 并处理消息。"""
    from framework.core.types import InputMessage

    descriptor = AgentDescriptor(
        address=AgentAddress(name="coder"),
        llm_config=AgentLLMConfig(),
        system_prompt_template="You are a coder.",
        context_strategy="persistent",
    )

    pool = AgentPool(broker=broker, agent_factory=factory)
    instance = await pool.register_resident(descriptor)

    # 给 coder 发送一条 envelope 消息
    envelope = AgentMessageEnvelope(
        payload={"content": "Write a function."},
        source=AgentAddress(name="user"),
        target=AgentAddress(name="coder"),
        conversation_id="conv1",
        agent_session_id="conv1:coder",
        message_type="agent_message",
    )
    await broker.send_to(envelope.target, envelope.to_broker_message())

    # 短暂等待消费循环处理（给 LLM 调用留出足够时间，但用极短的 timeout 让它快速失败）
    # 由于工厂创建了真实的 LiteLLMProvider，它会在 agent.run() 时尝试调用 LLM；
    # 但用户消息在 agent.run() 之前已保存，所以 1 秒足够完成 save()。
    await asyncio.sleep(1.0)

    # 验证 coder 的 context_manager 中已保存 agent 消息
    state = await instance.context_manager.load("conv1:coder")
    print("DEBUG state.history:", state.history)
    history_list = await state.history.to_list()
    agent_contents = [m["content"] for m in history_list if m.get("role") == "agent"]
    assert "Write a function." in agent_contents

    await pool.shutdown_all(timeout=1.0)


# ── 2. Agent 消息注入上下文（InboxFlushHook） ──

@pytest.mark.asyncio
async def test_inbox_flush_hook_injects_before_react_iteration(mock_provider):
    """InboxFlushHook 应在 ReActAgent turn 边界前将 inbox 消息注入 history。"""
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)

    # 预置两条 inbox 消息
    for content in ["msg1 from peer", "msg2 from peer"]:
        env = AgentMessageEnvelope(
            payload={"content": content},
            source=AgentAddress(name="peer"),
            target=AgentAddress(name="main"),
            message_type="agent_message",
            conversation_id="conv1",
            agent_session_id="conv1:main",
        )
        await producer.send("conv1:main", env)

    consumer = InboxConsumer(server=server)
    hook = InboxFlushHook(consumer=consumer, agent_name="main")
    mock_provider.set_responses([LLMResponse(content="done")])
    agent = ReActAgent(provider=mock_provider)

    from framework.memory.history import ListMessageHistory
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory([{"role": "user", "content": "start"}]),
        tool_manager=MagicMock(),
        max_iterations=1,
        metadata={"session_id": "conv1:main"},
        extensions={"hooks": [hook]},
    )

    emitter = BufferingEmitter[ReActEvent]()
    result = await agent.run(ctx, emitter)

    assert result.content == "done"
    # 验证 inbox 消息已被注入 history（InboxFlushHook 将每条消息作为独立 user 消息注入）
    history_list = await ctx.history.to_list()
    injected = [m for m in history_list if m.get("meta_inbox") is True]
    assert len(injected) == 2
    assert any("msg1 from peer" in m["content"] for m in injected)
    assert any("msg2 from peer" in m["content"] for m in injected)
    assert all(m.get("meta_source") == "peer" for m in injected)


# ── 3. 多 Agent 独立 Tool / Skill 配置 ──

@pytest.mark.asyncio
async def test_filtered_tool_manager_restricts_tools_per_agent():
    """不同 AgentDescriptor 的 allowed_tools 应产生独立的工具视图。"""
    base = InMemoryToolManager()

    class FakeTool(Tool):
        def __init__(self, name: str):
            super().__init__(name=name, description=name, parameters={"type": "object", "properties": {}}, config=ToolConfig())

        async def execute(self, **kwargs) -> str:
            return "ok"

    base.register(FakeTool("read_file"))
    base.register(FakeTool("write_file"))
    base.register(FakeTool("shell"))

    coder_tools = FilteredToolManager(base=base, allowed_tools=["read_file", "write_file"])
    viewer_tools = FilteredToolManager(base=base, allowed_tools=["read_file"])

    assert set(coder_tools.list_tools()) == {"read_file", "write_file"}
    assert set(viewer_tools.list_tools()) == {"read_file"}


@pytest.mark.asyncio
async def test_factory_assembles_distinct_tool_and_skill_managers(broker):
    """AgentFactory 应为不同 descriptor 组装出独立的 ToolManager 和 SkillManager 视图。"""
    from framework.core.skills import FileSkillSource, ProgressiveBuilder, SkillManager
    from framework.multi_agent.agent_skill_manager import AgentSkillManager
    from framework.multi_agent.factory import DefaultAgentFactory

    # 使用独立的 factory 实例，避免修改共享 fixture 状态
    factory = DefaultAgentFactory()

    skills_dir = Path(__file__).parent / "skills"
    skills_dir.mkdir(exist_ok=True)
    base_skill_mgr = SkillManager(
        source=FileSkillSource(directories=[skills_dir], cache=False),
        builder=ProgressiveBuilder(),
    )
    factory._skill_manager = base_skill_mgr

    base_tools = InMemoryToolManager()
    base_tools.register(
        Tool(
            name="coder_only_tool",
            description="coder",
            parameters={"type": "object", "properties": {}},
            config=ToolConfig(),
        )
    )

    coder_desc = AgentDescriptor(
        address=AgentAddress(name="coder"),
        allowed_tools=["coder_only_tool"],
        allowed_skills=["code_skill"],
    )
    planner_desc = AgentDescriptor(
        address=AgentAddress(name="planner"),
        allowed_tools=[],
        allowed_skills=[],
    )

    coder_inst = await factory.create_agent(coder_desc, mode="session", context_manager=InMemoryContextManager(), tool_manager=base_tools)
    planner_inst = await factory.create_agent(planner_desc, mode="session", context_manager=InMemoryContextManager(), tool_manager=base_tools)

    # Tool 视图独立
    assert "coder_only_tool" in coder_inst.tool_manager.list_tools()
    assert "coder_only_tool" not in planner_inst.tool_manager.list_tools()

    # Skill 视图独立
    assert isinstance(coder_inst.session._skill_manager, AgentSkillManager)
    assert isinstance(planner_inst.session._skill_manager, AgentSkillManager)


# ── 4. 实时/初始干预能力 ──

@pytest.mark.asyncio
async def test_task_supervisor_cancels_slow_subagent_via_timeout_policy():
    """TaskSupervisor 应在 TimeoutCancellationPolicy 触发时硬中断子任务。"""
    coord = InMemoryTaskCoordinator()
    record = TaskRecord(task_id="sub1", task_type="subagent", created_at=time.time())
    await coord.register_task("sub1", record)
    await coord.bind_policy("sub1", TimeoutCancellationPolicy.from_duration(0.1))

    supervisor = TaskSupervisor(coord, check_interval=0.05)

    async def _slow_subagent():
        await asyncio.sleep(10)
        return AgentResult(content="done")

    with pytest.raises(asyncio.CancelledError):
        await supervisor.supervise("sub1", _slow_subagent())

    # 验证状态更新为 cancelled
    rec = await coord.get_task_record("sub1")
    assert rec.status == "cancelled"


@pytest.mark.asyncio
async def test_task_intervention_hook_cancels_react_iteration():
    """TaskInterventionHook 应在 ReAct 迭代前检查策略并抛出 CancelledError。"""
    coord = InMemoryTaskCoordinator()
    record = TaskRecord(task_id="turn1", task_type="turn", created_at=time.time())
    await coord.register_task("turn1", record)
    await coord.bind_policy("turn1", TimeoutCancellationPolicy.from_duration(0.05))

    hook = TaskInterventionHook("turn1", coord)
    mock_provider = MockStreamingProvider()
    mock_provider.set_responses([LLMResponse(content="should not reach")])
    agent = ReActAgent(provider=mock_provider)

    from framework.memory.history import ListMessageHistory
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory([{"role": "user", "content": "start"}]),
        tool_manager=MagicMock(),
        max_iterations=3,
        extensions={"hooks": [hook]},
    )

    # 稍微等待让策略 deadline 过期
    await asyncio.sleep(0.1)

    runner = InterruptibleRunner()
    emitter = BufferingEmitter[ReActEvent]()
    result = await runner.run(agent, ctx, emitter)

    assert result.stop_reason == "cancelled"


# ── 5. 反馈与上报能力（TaskEventBus -> 平台中心） ──

@pytest.mark.asyncio
async def test_subagent_lifecycle_emits_events_to_platform(memory_event_reporter):
    """Subagent 生命周期应通过 TaskEventBus 向平台中心上报 REGISTERED / STARTED / CANCELLED 等事件。"""
    reporter = CompositeTaskEventReporter([memory_event_reporter])
    bus = TaskEventBus(reporter)
    coord = InMemoryTaskCoordinator(event_bus=bus)
    supervisor = TaskSupervisor(coord, check_interval=0.05, emit_heartbeat=True)

    record = TaskRecord(task_id="sub_life", task_type="subagent", created_at=time.time())
    await coord.register_task("sub_life", record)
    await coord.bind_policy("sub_life", TimeoutCancellationPolicy.from_duration(0.2))

    async def _slow():
        await asyncio.sleep(10)
        return "ok"

    with pytest.raises(asyncio.CancelledError):
        await supervisor.supervise("sub_life", _slow())

    # 等待 heartbeat 协程再发一轮
    await asyncio.sleep(0.15)

    events = memory_event_reporter.events
    types = {e.event_type for e in events}

    assert TaskEventType.REGISTERED in types
    assert TaskEventType.STARTED in types
    assert TaskEventType.CANCELLED in types
    assert TaskEventType.HEARTBEAT in types or TaskEventType.POLICY_TRIGGERED in types


@pytest.mark.asyncio
async def test_task_progress_hook_reports_to_platform_during_react(broker, memory_event_reporter):
    """TaskProgressHook 应在 ReAct 迭代中向平台中心上报 PROGRESS 事件。"""
    reporter = CompositeTaskEventReporter([memory_event_reporter])
    bus = TaskEventBus(reporter)
    progress_hook = TaskProgressHook("turn_p", bus)

    mock_provider = MockStreamingProvider()
    # 两轮：第一轮带 tool call，第二轮纯文本结束
    mock_provider.set_responses([
        LLMResponse(content="thinking", tool_calls=[ToolCall(tool_name="calc", arguments={}, call_id="t1")]),
        LLMResponse(content="final"),
    ])

    agent = ReActAgent(provider=mock_provider)
    tm = InMemoryToolManager()
    class CalcTool(Tool):
        async def execute(self, **kwargs) -> str:
            return "42"

    tm.register(
        CalcTool(
            name="calc",
            description="calc",
            parameters={"type": "object", "properties": {}},
            config=ToolConfig(),
        )
    )

    from framework.memory.history import ListMessageHistory
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory([{"role": "user", "content": "1+1"}]),
        tool_manager=tm,
        max_iterations=3,
        extensions={"hooks": [progress_hook]},
    )

    emitter = BufferingEmitter[ReActEvent]()
    await agent.run(ctx, emitter)

    progress_events = [e for e in memory_event_reporter.events if e.event_type == TaskEventType.PROGRESS]
    assert len(progress_events) >= 1
    # 至少有一次 PROGRESS 包含 iteration / tool_calls
    assert any("iteration" in e.payload for e in progress_events)


@pytest.mark.asyncio
async def test_platform_receives_policy_triggered_and_heartbeat_events(memory_event_reporter):
    """TaskSupervisor 心跳和策略触发应向平台上报 HEARTBEAT / POLICY_TRIGGERED 事件。"""

    class NotifyPolicy:
        policy_type = "notify"

        async def check(self, task_record: TaskRecord):
            from framework.multi_agent.intervention import InterventionAction, InterventionResult

            return InterventionResult(action=InterventionAction.NOTIFY, reason="threshold reached")

    reporter = CompositeTaskEventReporter([memory_event_reporter])
    bus = TaskEventBus(reporter)
    coord = InMemoryTaskCoordinator(event_bus=bus)
    supervisor = TaskSupervisor(coord, check_interval=0.05, emit_heartbeat=True)

    record = TaskRecord(task_id="t_notify", task_type="test", created_at=time.time())
    await coord.register_task("t_notify", record)
    # 绑定一个 NOTIFY 策略，不应取消任务
    from framework.multi_agent.intervention import TaskInterventionPolicy

    class _Notify(TaskInterventionPolicy):
        policy_type = "notify"

        async def check(self, task_record):
            from framework.multi_agent.intervention import InterventionAction, InterventionResult

            return InterventionResult(action=InterventionAction.NOTIFY, reason="threshold")

    await coord.bind_policy("t_notify", _Notify())

    async def _quick():
        await asyncio.sleep(0.15)
        return "done"

    result = await supervisor.supervise("t_notify", _quick())
    assert result == "done"

    events = memory_event_reporter.events
    types = {e.event_type for e in events}
    assert TaskEventType.HEARTBEAT in types
    assert TaskEventType.POLICY_TRIGGERED in types


# ── 6. 端到端：Hook 组合 + 干预 + 取消 ──

@pytest.mark.asyncio
async def test_composite_run_hook_with_inbox_and_intervention(broker, mock_provider):
    """CompositeRunHook 应能同时运行 InboxFlushHook 和 TaskInterventionHook。"""
    server = InMemoryInboxServer()
    producer = InboxProducer(server=server)
    consumer = InboxConsumer(server=server)

    # 预置 inbox 消息
    env = AgentMessageEnvelope(
        payload={"content": "injected"},
        source=AgentAddress(name="peer"),
        target=AgentAddress(name="main"),
        message_type="agent_message",
        conversation_id="c1",
        agent_session_id="c1:main",
    )
    await producer.send("c1:main", env)

    inbox_hook = InboxFlushHook(consumer=consumer, agent_name="main")

    # intervention hook 不 cancel（使用很远的 deadline）
    coord = InMemoryTaskCoordinator()
    record = TaskRecord(task_id="turn_ok", task_type="turn", created_at=time.time())
    await coord.register_task("turn_ok", record)
    await coord.bind_policy("turn_ok", TimeoutCancellationPolicy(deadline=time.time() + 100))
    intervention_hook = TaskInterventionHook("turn_ok", coord)

    from framework.hook import HookRunner, HookSpec, HookErrorPolicy
    runner = HookRunner([
        HookSpec(hook=inbox_hook, on_error=HookErrorPolicy.LOG),
        HookSpec(hook=intervention_hook, on_error=HookErrorPolicy.LOG),
    ])

    mock_provider.set_responses([LLMResponse(content="ack")])
    agent = ReActAgent(provider=mock_provider)

    from framework.memory.history import ListMessageHistory
    ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory([{"role": "user", "content": "start"}]),
        tool_manager=MagicMock(),
        max_iterations=1,
        metadata={"session_id": "c1:main"},
        extensions={
            "hooks": [inbox_hook, intervention_hook],
            "hook_runner": runner,
        },
    )

    emitter = BufferingEmitter[ReActEvent]()
    result = await agent.run(ctx, emitter)
    assert result.content == "ack"

    history_list = await ctx.history.to_list()
    injected = [m for m in history_list if m.get("meta_inbox") is True]
    assert len(injected) == 1
    assert "injected" in injected[0]["content"]
