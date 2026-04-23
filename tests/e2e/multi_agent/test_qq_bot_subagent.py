"""End-to-end test for QQ Bot spawning and receiving subagent results.

验证:
- QQ Bot 主服务初始化后具备 spawn_subagent 工具
- spawn_subagent 工具被调用后能返回 Subagent 执行结果
- 结果通过 Broker 正确回传到父 Agent
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"))


@pytest.mark.asyncio
async def test_bot_service_has_spawn_subagent_tool():
    """QQ Bot 服务初始化后，tool_manager 应包含 spawn_subagent 和 send_message。"""
    try:
        from qq_adapters import QQInputAdapter, QQOutputAdapter
        from bot_service import BotService

        input_adapter = MagicMock(spec=QQInputAdapter)
        input_adapter.name = "mock_qq"
        output_adapter = MagicMock(spec=QQOutputAdapter)
        output_adapter.name = "mock_qq"

        def emitter_factory(sid):
            return MagicMock()

        # 使用 mock 配置避免真实 LLM 初始化
        mock_config = {
            "qq": {"app_id": "x", "secret": "x", "sandbox": True, "allow_from": ["*"]},
            "llm": {"model": "mock", "api_key": "x", "temperature": 0.7, "max_tokens": 100},
            "agent": {"system_prompt": "test", "max_iterations": 5},
            "memory": {"short_term": {"strategy": "buffer", "max_messages": 10, "budget_ratio": 0.5}},
            "mcp": {"servers": {}},
            "multi_agent": {
                "enabled": True,
                "parent_agent_name": "main",
                "subagent": {
                    "enabled": True,
                    "name": "helper",
                    "system_prompt": "You are a helper.",
                },
            },
            "tools": {
                "file_tools": {"enabled": False},
                "shell_tools": {"enabled": False},
                "mcp_tools": {"enabled": False},
            },
            "output": {"streaming": True, "preset": "minimal"},
            "sessions": {"workspace": "./data/sessions", "auto_save": True},
        }

        service = BotService(
            config_dir=Path("./examples/bot_project/config"),
            input_adapter=input_adapter,
            output_adapter=output_adapter,
            emitter_factory=emitter_factory,
        )
        service.config = mock_config

        # 部分初始化：跳过真实 LLM/MemorySystem，只验证工具注册
        from framework.core.tool_manager import InMemoryToolManager
        from framework.messaging.broker_memory import InMemoryMessageBroker
        from framework.multi_agent import (
            DefaultAgentFactory,
            SubagentManager,
            TaskCoordinationConfig,
        )

        service.broker = InMemoryMessageBroker()
        await service.broker.start()
        service.tool_manager = InMemoryToolManager()
        service.agent_factory = DefaultAgentFactory()
        service.provider = MagicMock()  # mock LLM provider for subagent memory creation
        service.subagent_manager = SubagentManager(
            broker=service.broker,
            agent_factory=service.agent_factory,
            coordination_config=TaskCoordinationConfig(enable_for_subagent=False),
        )

        await service._register_multi_agent_tools()
        tools = service.tool_manager.list_tools()

        assert "spawn_subagent" in tools
        assert "send_message" in tools

    except ImportError as e:
        pytest.skip(f"QQBotService dependencies not available: {e}")


@pytest.mark.asyncio
async def test_spawn_subagent_tool_returns_result():
    """spawn_subagent 工具执行后应直接返回字符串结果。"""
    try:
        from bot_service import SpawnSubagentTool

        from framework.core.emitter import AgentResult
        from framework.core.tool_manager import InMemoryToolManager
        from framework.messaging.broker_memory import InMemoryMessageBroker
        from framework.multi_agent import (
            AgentAddress,
            AgentDescriptor,
            AgentLLMConfig,
            DefaultAgentFactory,
            SubagentManager,
            TaskCoordinationConfig,
        )
        from framework.multi_agent.descriptor import ContextGovernanceConfig

        broker = InMemoryMessageBroker()
        factory = MagicMock(spec=DefaultAgentFactory)
        fake_instance = MagicMock()
        fake_instance.session.process_message = AsyncMock(return_value=AgentResult(content="subagent_done"))
        fake_instance.tool_manager = InMemoryToolManager()
        factory.create_agent = AsyncMock(return_value=fake_instance)

        mgr = SubagentManager(
            broker=broker,
            agent_factory=factory,
            coordination_config=TaskCoordinationConfig(enable_for_subagent=False),
        )

        descriptor = AgentDescriptor(
            address=AgentAddress(name="helper"),
            llm_config=AgentLLMConfig(),
            system_prompt_template="You are a helper.",
            governance_config=ContextGovernanceConfig(),
        )

        tool = SpawnSubagentTool(
            manager=mgr,
            default_parent_address=AgentAddress(name="main"),
            descriptor=descriptor,
        )

        result = await tool.execute(task_prompt="test task", conversation_id="conv_qq_1")
        assert "subagent_done" in result

    except ImportError as e:
        pytest.skip(f"Dependencies not available: {e}")
