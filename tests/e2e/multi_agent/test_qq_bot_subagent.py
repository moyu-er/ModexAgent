"""End-to-end checks for QQ Bot subagent dispatch wiring."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(
    0,
    str(Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"),
)


@pytest.mark.asyncio
async def test_bot_service_has_dispatch_task_tool() -> None:
    """Pool-mode bot exposes async subagent dispatch tools, not legacy spawn."""
    try:
        from bot_service import BotService

        from framework.core.tool_manager import InMemoryToolManager
        from framework.ioc.configs.agent import AgentConfig
        from framework.ioc.configs.app import AppConfig
        from framework.messaging.broker_memory import InMemoryMessageBroker
        from framework.multi_agent import CommunicationTracker
        from framework.multi_agent.tools import DispatchTaskTool, SendMessageAsyncTool
    except ImportError as exc:
        pytest.skip(f"QQBotService dependencies not available: {exc}")

    service = BotService(
        config_dir=Path("./examples/bot_project/config"),
        input_adapter=MagicMock(name="input_adapter"),
        output_adapter=MagicMock(name="output_adapter"),
        emitter_factory=MagicMock(),
        mode="pool",
        app_config=AppConfig(
            llm={"model": "mock", "api_key": "key"},
            agents=[
                AgentConfig(name="main", role="main"),
                AgentConfig(name="helper", role="subagent"),
            ],
        ),
    )
    service.tool_manager = InMemoryToolManager()
    service.broker = InMemoryMessageBroker()
    await service.broker.start()
    service.agent_bus = MagicMock()
    service.agent_pool = MagicMock()
    service.subagent_service = MagicMock()
    service.communication_tracker = CommunicationTracker()

    await service._register_multi_agent_tools()
    tools = service.tool_manager.list_tools()

    assert "send_message" in tools
    assert "send_message_async" in tools
    assert "dispatch_task" in tools
    assert "spawn_subagent" not in tools
    assert isinstance(service.tool_manager.get_tool("dispatch_task"), DispatchTaskTool)
    assert isinstance(
        service.tool_manager.get_tool("send_message_async"),
        SendMessageAsyncTool,
    )
    await service.broker.stop()


@pytest.mark.asyncio
async def test_dispatch_task_tool_returns_invocation_id() -> None:
    """dispatch_task queues a task and tracks the pending invocation."""
    from framework.messaging.broker_memory import InMemoryMessageBroker
    from framework.multi_agent import AgentAddress, CommunicationTracker
    from framework.multi_agent.tools import DispatchTaskTool

    broker = InMemoryMessageBroker()
    await broker.start()
    tracker = CommunicationTracker()
    tool = DispatchTaskTool(
        broker=broker,
        self_address=AgentAddress(name="main"),
        allowed_targets=["helper"],
        comm_tracker=tracker,
    )

    result = await tool.execute(target_agent="helper", task_prompt="test task")

    assert "Task dispatched to helper" in result
    assert "invocation_id: inv_" in result
    assert len(tracker.get_pending_for_agent("main")) == 1
    await broker.stop()
