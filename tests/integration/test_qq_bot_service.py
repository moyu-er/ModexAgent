"""Integration tests for bot_service.py components.

验证端到端流程：
- QQBotService 组件初始化
- QQBotEmitter 与 QQOutputAdapter 的集成
- 流式/非流式模式切换
- 推理内容处理流程
"""

import pytest

pytestmark = pytest.mark.integration
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Add framework path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class TestQQBotServiceIntegration:
    """Integration tests for QQ Bot Service."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        return {
            "qq": {
                "app_id": "test_app_id",
                "secret": "test_secret",
                "sandbox": True,
                "allow_from": ["*"],
            },
            "llm": {
                "model": "deepseek-ai/DeepSeek-R1",
                "api_key": "test_key",
                "temperature": 0.7,
                "max_tokens": 2000,
            },
            "agent": {
                "system_prompt": "You are a helpful assistant.",
            },
            "output": {
                "streaming": True,
            },
            "tools": {
                "file_tools": {"enabled": False},
                "shell_tools": {"enabled": False},
            },
            "mcp": {"servers": {}},
        }

    @pytest.fixture
    def mock_qq_client(self):
        """Create mock QQ bot client."""
        client = MagicMock()
        client.api = MagicMock()
        client.api.post_c2c_message = AsyncMock()
        return client

    def test_streaming_aware_emitter_import(self):
        """Test that StreamingAwareEmitter can be imported."""
        from framework import StreamingAwareEmitter, ReActEvent

        # Should be able to create a subclass
        class TestEmitter(StreamingAwareEmitter[ReActEvent]):
            async def emit_delta(self, delta: str) -> None:
                pass

        assert TestEmitter is not None

    def test_react_event_has_model_reasoning(self):
        """Test that ReActEvent includes MODEL_REASONING."""
        from framework.agents.react import ReActEvent

        assert hasattr(ReActEvent, "MODEL_REASONING")
        assert ReActEvent.MODEL_REASONING.value == "model_reasoning"

    def test_agent_result_has_reasoning_field(self):
        """Test that AgentResult has reasoning field."""
        from framework.core.emitter import AgentResult

        result = AgentResult(content="Hello", reasoning="Thinking...")
        assert result.reasoning == "Thinking..."

    @pytest.mark.asyncio
    async def test_qb_bot_emitter_business_logic(self, caplog):
        """Test QQBotEmitter business logic in isolation."""
        import sys
        sys.path.insert(0, 'F:\\tool\\pythonProject\\multiDemo\\backend\\app\\framework\\examples\\bot_project')

        try:
            from qq_adapters import QQBotEmitter
            from framework.agents.react import ReActEvent
            from framework.core.emitter import AgentResult, ToolCall

            # Create mock adapter
            mock_adapter = MagicMock()
            mock_adapter.supports_streaming = False
            mock_adapter.send_delta = AsyncMock()
            mock_adapter.send = AsyncMock()
            mock_adapter.flush_deltas = AsyncMock()

            # Create emitter
            emitter = QQBotEmitter(
                output_adapter=mock_adapter,
                session_id="test_session",
            )

            # Test content is buffered (pseudo-streaming)
            await emitter.emit_delta("Hello ")
            await emitter.emit_delta("World")
            assert emitter._content_buffer == "Hello World"
            assert len(mock_adapter.send_delta.call_args_list) == 0

            # Test reasoning is logged (not sent)
            import logging
            with caplog.at_level(logging.INFO, logger='qq_bot.reasoning'):
                await emitter.emit(ReActEvent.MODEL_REASONING, "Thinking...")
                assert "[Reasoning]" in caplog.text

            # Test tool calls are ignored
            tool_call = ToolCall(tool_name="test", arguments={})
            await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)
            # No additional calls to adapter

            # Test complete flushes buffer via send()
            result = AgentResult(content="Hello World", reasoning="Some reasoning")
            await emitter.emit_complete(result)
            assert mock_adapter.send.called

        except ImportError as e:
            pytest.skip(f"QQBotEmitter not available: {e}")

    @pytest.mark.asyncio
    async def test_react_agent_streaming_vs_non_streaming(self):
        """Test ReActAgent correctly switches between streaming and non-streaming based on emitter."""
        from framework.agents.react import ReActAgent, ReActEvent
        from framework.core.agent import AgentContext
        from framework.core.emitter import BufferingEmitter
        from framework.core.provider import StreamingLLMProvider
        from framework.core.types import LLMResponse

        # Create mock provider that tracks which API is called
        class MockProvider(StreamingLLMProvider):
            def __init__(self):
                self.chat_stream_called = False
                self.chat_called = False

            async def chat_stream(self, messages=None, on_content_delta=None, on_reasoning_delta=None, **kwargs):
                self.chat_stream_called = True
                if on_content_delta:
                    await on_content_delta("Hello")
                return LLMResponse(content="Hello")

            async def chat(self, messages=None, **kwargs):
                self.chat_called = True
                return LLMResponse(content="Hello")

            def get_default_model(self):
                return "mock-model"

        provider = MockProvider()
        agent = ReActAgent(provider=provider)

        from framework.memory.history import ListMessageHistory
        context = AgentContext(
            system_prompt="Test",
            history=ListMessageHistory([{"role": "user", "content": "Hi"}]),
            tool_manager=MagicMock(),
        )

        # Test streaming mode (emitter wants streaming)
        class StreamingEmitter(BufferingEmitter[ReActEvent]):
            def wants_streaming(self):
                return True

        emitter = StreamingEmitter()
        result = await agent.run(context, emitter)
        assert provider.chat_stream_called is True
        assert provider.chat_called is False

        # Reset
        provider.chat_stream_called = False
        provider.chat_called = False

        # Test non-streaming mode (emitter doesn't want streaming)
        emitter2 = BufferingEmitter[ReActEvent]()
        result = await agent.run(context, emitter2)
        assert provider.chat_stream_called is False
        assert provider.chat_called is True

    def test_output_adapter_send_delta_interface(self):
        """Test that OutputAdapter has the send_delta interface."""
        from framework.pipeline.adapters import OutputAdapter

        # Check that send_delta method exists
        assert hasattr(OutputAdapter, "send_delta")
        assert hasattr(OutputAdapter, "flush_deltas")
        assert hasattr(OutputAdapter, "supports_streaming")

    @pytest.mark.asyncio
    async def test_end_to_end_event_flow(self):
        """Test complete event flow from Agent to QQ Output."""
        from framework.agents.react import ReActAgent, ReActEvent
        from framework.core.agent import AgentContext
        from framework.core.emitter import StreamingAwareEmitter, AgentResult

        # Track events
        events_received = []

        class MockAdapter:
            def __init__(self):
                self.supports_streaming = False

            async def send_delta(self, delta, session_id, metadata=None):
                events_received.append(("send_delta", delta))

            async def send(self, message, session_id):
                events_received.append(("send", message.content))

            async def flush_deltas(self, session_id):
                events_received.append(("flush",))

        class TestEmitter(StreamingAwareEmitter[ReActEvent]):
            async def emit_delta(self, delta: str) -> None:
                events_received.append(("emit_delta", delta))
                await self.output_adapter.send_delta(delta, self.session_id)

            async def _on_event(self, event: ReActEvent, data=None) -> None:
                event_name = event.value if hasattr(event, "value") else str(event)
                if event_name == "model_reasoning":
                    events_received.append(("model_reasoning", data))
                await super()._on_event(event, data)

        # Setup
        adapter = MockAdapter()
        emitter = TestEmitter(adapter, "test_session")

        # Create mock provider
        from framework.core.types import LLMResponse
        class MockProvider:
            async def chat(self, **kwargs):
                return LLMResponse(
                    content="Final answer",
                    reasoning_content="My reasoning",
                )

            def get_default_model(self):
                return "mock"

        agent = ReActAgent(provider=MockProvider())
        from framework.memory.history import ListMessageHistory
        context = AgentContext(
            system_prompt="Test",
            history=ListMessageHistory([{"role": "user", "content": "Hi"}]),
            tool_manager=MagicMock(),
        )

        # Run
        result = await agent.run(context, emitter)

        # Verify flow
        assert result.content == "Final answer"
        assert result.reasoning == "My reasoning"

        # Should have received reasoning event
        reasoning_events = [e for e in events_received if e[0] == "model_reasoning"]
        assert len(reasoning_events) == 1
        assert reasoning_events[0][1] == "My reasoning"

    def test_qq_service_initialization_structure(self, mock_config):
        """Test QQBotService structure without actually initializing."""
        import sys
        sys.path.insert(0, 'F:\\tool\\pythonProject\\multiDemo\\backend\\app\\framework\\examples\\bot_project')

        try:
            from bot_service import QQBotService
            from qq_adapters import QQBotEmitter

            # Verify that emitter_factory pattern is supported
            assert hasattr(QQBotService, "_load_config")

        except ImportError as e:
            pytest.skip(f"QQBotService not importable: {e}")

    @pytest.mark.asyncio
    async def test_qq_bot_skills_use_compact_prompt(self):
        """Regression test: QQ Bot skills must produce a compact prompt table,
        not inline full skill content, to avoid exceeding LLM context limits.
        """
        import sys
        sys.path.insert(0, 'F:\\tool\\pythonProject\\multiDemo\\backend\\app\\framework\\examples\\bot_project')

        from pathlib import Path
        from framework.core.skills import (
            FileSkillSource,
            ProgressiveBuilder,
            ResolutionContext,
            SkillManager,
        )

        skills_dir = Path('examples/bot_project/skills/main')
        if not skills_dir.exists():
            pytest.skip("bot_project/skills/main directory not found")

        source = FileSkillSource(
            directories=[skills_dir],
            cache=True,
            layout="directory",
            skill_filename="SKILL.md",
        )
        sm = SkillManager(source=source, builder=ProgressiveBuilder())

        class FakeTM:
            def has_tool(self, name: str) -> bool:
                return name == "read_file"

        ctx = ResolutionContext(tool_manager=FakeTM())
        prompt = await sm.build_prompt(ctx)

        # Should be a compact table, not inlined content
        assert "| Skill | Description | Location |" in prompt
        # Must NOT contain full skill body text that would bloat the context
        assert "# Weather" not in prompt
        assert "# Memory" not in prompt
        assert "# Summarize" not in prompt
        assert "# GitHub Skill" not in prompt
        # Each skill should appear as a single table row, not as multi-line content
        assert prompt.count("## Skills") == 1


    @pytest.mark.asyncio
    async def test_bot_service_pool_mode_bridge_routing(self, tmp_path):
        """BotService(mode='pool') 通过 BrokerBridgeService 正确路由输入/输出。"""
        import sys
        from pathlib import Path
        from unittest.mock import patch, AsyncMock

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot_service import BotService
        from framework.core.types import InputMessage, LLMResponse
        from framework.pipeline.adapters import InputAdapter, OutputAdapter, OutputMessage

        class _MockInputAdapter(InputAdapter):
            def __init__(self):
                self._queue = asyncio.Queue()
                self._running = False

            @property
            def name(self):
                return "mock_input"

            async def start(self):
                self._running = True

            async def stop(self):
                self._running = False

            def receive(self):
                async def _gen():
                    while self._running:
                        try:
                            msg = await asyncio.wait_for(self._queue.get(), timeout=0.05)
                            yield msg
                        except TimeoutError:
                            pass
                return _gen()

            async def inject(self, msg: InputMessage):
                await self._queue.put(msg)

        class _MockOutputAdapter(OutputAdapter):
            def __init__(self):
                self.messages: list[tuple[OutputMessage, str]] = []

            @property
            def name(self):
                return "mock_output"

            @property
            def supports_streaming(self):
                return False

            async def send(self, message: OutputMessage, session_id: str):
                self.messages.append((message, session_id))

            async def send_delta(self, delta: str, session_id: str, metadata=None):
                pass

            async def flush_deltas(self, session_id: str):
                pass

        class _MockProvider:
            async def chat(self, messages=None, **kwargs):
                return LLMResponse(content="pong")

            def get_default_model(self):
                return "mock-model"

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_yaml = config_dir / "bot_config.yml"
        config_yaml.write_text(
            """
llm:
  model: mock-model
  api_key: test-key
  base_url: ""
  temperature: 0.7
  max_tokens: 100

agent:
  system_prompt: "You are a test agent."
  max_iterations: 1

memory:
  short_term:
    max_messages: 10
    max_tokens: 100
    budget_ratio: 0.5

multi_agent:
  parent_agent_name: main
  enabled: false

tools:
  file_tools:
    enabled: false
  shell_tools:
    enabled: false

mcp:
  servers: {}
""",
            encoding="utf-8",
        )

        input_adapter = _MockInputAdapter()
        output_adapter = _MockOutputAdapter()

        def _emitter_factory(session_id: str):
            from framework.core.emitter import BufferingEmitter
            from framework.agents.react import ReActEvent
            return BufferingEmitter[ReActEvent]()

        with patch.object(BotService, "_create_provider", return_value=_MockProvider()):
            service = BotService(
                config_dir=config_dir,
                input_adapter=input_adapter,
                output_adapter=output_adapter,
                emitter_factory=_emitter_factory,
                mode="pool",
            )
            await service.initialize()

            # 启动 pool 模式（只启动 bridge，不阻塞）
            start_task = asyncio.create_task(service.start())
            await asyncio.sleep(0.1)

            # 注入一条消息
            await input_adapter.inject(InputMessage(content="ping", session_id="s1"))

            # 等待消息流转
            for _ in range(50):
                if output_adapter.messages:
                    break
                await asyncio.sleep(0.05)

            assert len(output_adapter.messages) >= 1
            assert "pong" in output_adapter.messages[0][0].content

            # 停止服务
            service._shutdown_event.set()
            await asyncio.wait_for(start_task, timeout=2.0)
            await service.stop()

    @pytest.mark.asyncio
    async def test_bot_service_pipeline_mode_memory_system_initialized(self, tmp_path):
        """BotService(mode='pipeline') 初始化后 MemorySystem 和 ContextManager 应正确创建。"""
        import sys
        from pathlib import Path
        from unittest.mock import patch

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot_service import BotService
        from framework.core.types import LLMResponse
        from framework.pipeline.adapters import InputAdapter, OutputAdapter, OutputMessage

        class _MockInputAdapter(InputAdapter):
            @property
            def name(self): return "mock_input"
            async def start(self): pass
            async def stop(self): pass
            def receive(self):
                async def _gen():
                    if False:
                        yield None
                return _gen()

        class _MockOutputAdapter(OutputAdapter):
            @property
            def name(self): return "mock_output"
            @property
            def supports_streaming(self): return False
            async def send(self, message: OutputMessage, session_id: str): pass
            async def send_delta(self, delta: str, session_id: str, metadata=None): pass
            async def flush_deltas(self, session_id: str): pass

        class _MockProvider:
            async def chat(self, messages=None, **kwargs):
                return LLMResponse(content="ok")
            def get_default_model(self): return "mock"

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "bot_config.yml").write_text(
            """
llm:
  model: mock
  api_key: key
  temperature: 0.7
agent:
  system_prompt: "test"
memory:
  short_term:
    max_messages: 5
    max_tokens: 50
    budget_ratio: 0.5
multi_agent:
  enabled: false
tools:
  file_tools:
    enabled: false
  shell_tools:
    enabled: false
mcp:
  servers: {}
""",
            encoding="utf-8",
        )

        with patch.object(BotService, "_create_provider", return_value=_MockProvider()):
            service = BotService(
                config_dir=config_dir,
                input_adapter=_MockInputAdapter(),
                output_adapter=_MockOutputAdapter(),
                emitter_factory=lambda sid: None,
                mode="pipeline",
            )
            await service.initialize()

            assert service.memory_system is not None
            assert service.context_manager is not None
            assert service.inbox_server is not None
            assert service.inbox_producer is not None
            assert service.inbox_consumer is not None
            assert service.pipeline is not None
            assert service.subagent_manager is not None

            await service.stop()

    @pytest.mark.asyncio
    async def test_spawn_subagent_tool_delegates_to_manager(self):
        """SpawnSubagentTool 应正确将参数传递给 SubagentService.spawn_and_wait。"""
        import sys
        from pathlib import Path

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot_service import SpawnSubagentTool
        from framework.multi_agent import SubagentService, AgentDescriptor, AgentAddress
        from framework.core.emitter import AgentResult

        manager = AsyncMock(spec=SubagentService)
        manager.spawn_and_wait = AsyncMock(return_value=AgentResult(content="done"))

        descriptor = AgentDescriptor(address=AgentAddress(name="helper"))
        parent_address = AgentAddress(name="main")
        tool = SpawnSubagentTool(
            manager=manager,
            default_parent_address=parent_address,
            descriptor=descriptor,
        )

        result = await tool.execute(task_prompt="solve this", conversation_id="conv_123")
        assert result == "done"
        manager.spawn_and_wait.assert_awaited_once()
        call_kwargs = manager.spawn_and_wait.call_args.kwargs
        assert call_kwargs["task_prompt"] == "solve this"
        assert call_kwargs["conversation_id"] == "conv_123"
        assert call_kwargs["parent_address"] == parent_address
        assert call_kwargs["descriptor"] == descriptor

    @pytest.mark.asyncio
    async def test_bot_service_stop_cleans_up_resources(self, tmp_path):
        """stop() 应正确清理 pipeline、broker、agent_bus、subagent_manager 等资源。"""
        import sys
        from pathlib import Path
        from unittest.mock import patch

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot_service import BotService
        from framework.core.types import LLMResponse
        from framework.pipeline.adapters import InputAdapter, OutputAdapter, OutputMessage

        class _MockInputAdapter(InputAdapter):
            @property
            def name(self): return "mock_input"
            async def start(self): pass
            async def stop(self): pass
            def receive(self):
                async def _gen():
                    if False:
                        yield None
                return _gen()

        class _MockOutputAdapter(OutputAdapter):
            @property
            def name(self): return "mock_output"
            @property
            def supports_streaming(self): return False
            async def send(self, message: OutputMessage, session_id: str): pass
            async def send_delta(self, delta: str, session_id: str, metadata=None): pass
            async def flush_deltas(self, session_id: str): pass

        class _MockProvider:
            async def chat(self, messages=None, **kwargs):
                return LLMResponse(content="ok")
            def get_default_model(self): return "mock"

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "bot_config.yml").write_text(
            """
llm:
  model: mock
  api_key: key
agent:
  system_prompt: "test"
multi_agent:
  enabled: false
tools:
  file_tools:
    enabled: false
  shell_tools:
    enabled: false
mcp:
  servers: {}
""",
            encoding="utf-8",
        )

        with patch.object(BotService, "_create_provider", return_value=_MockProvider()):
            service = BotService(
                config_dir=config_dir,
                input_adapter=_MockInputAdapter(),
                output_adapter=_MockOutputAdapter(),
                emitter_factory=lambda sid: None,
                mode="pipeline",
            )
            await service.initialize()

            # 启动 pipeline 后再停止
            run_task = asyncio.create_task(service.pipeline.run())
            await asyncio.sleep(0.05)
            await service.stop()

            # 验证 pipeline 已停止
            assert service.pipeline._running is False
            # 验证 broker 已停止（InMemoryMessageBroker 内部 _running=False）
            assert service.broker._running is False

    @pytest.mark.asyncio
    async def test_bot_service_pipeline_registers_multi_agent_tools(self, tmp_path):
        """pipeline 模式下 initialize 后应多 Agent 工具被正确注册到 ToolManager。"""
        import sys
        from pathlib import Path
        from unittest.mock import patch

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot_service import BotService
        from framework.core.types import LLMResponse
        from framework.pipeline.adapters import InputAdapter, OutputAdapter, OutputMessage

        class _MockInputAdapter(InputAdapter):
            @property
            def name(self): return "mock_input"
            async def start(self): pass
            async def stop(self): pass
            def receive(self):
                async def _gen():
                    if False:
                        yield None
                return _gen()

        class _MockOutputAdapter(OutputAdapter):
            @property
            def name(self): return "mock_output"
            @property
            def supports_streaming(self): return False
            async def send(self, message: OutputMessage, session_id: str): pass
            async def send_delta(self, delta: str, session_id: str, metadata=None): pass
            async def flush_deltas(self, session_id: str): pass

        class _MockProvider:
            async def chat(self, messages=None, **kwargs):
                return LLMResponse(content="ok")
            def get_default_model(self): return "mock"

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "bot_config.yml").write_text(
            """
llm:
  model: mock
  api_key: key
agent:
  system_prompt: "test"
multi_agent:
  enabled: true
  parent_agent_name: main
  subagent_sync:
    enabled: true
    name: helper
  subagent_async:
    enabled: true
    name: helper
tools:
  file_tools:
    enabled: false
  shell_tools:
    enabled: false
mcp:
  servers: {}
""",
            encoding="utf-8",
        )

        with patch.object(BotService, "_create_provider", return_value=_MockProvider()):
            service = BotService(
                config_dir=config_dir,
                input_adapter=_MockInputAdapter(),
                output_adapter=_MockOutputAdapter(),
                emitter_factory=lambda sid: None,
                mode="pipeline",
            )
            await service.initialize()

            tool_names = service.tool_manager.list_tools()
            assert "send_message" in tool_names
            assert "spawn_subagent" in tool_names or "spawn_subagent_sync" in tool_names

            await service.stop()

    @pytest.mark.asyncio
    async def test_bot_service_pool_registers_subagent_residents(self, tmp_path):
        """pool 模式下 initialize 后子 Agent 应被注册为 AgentPool 常驻代理。"""
        import sys
        from pathlib import Path
        from unittest.mock import patch

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot_service import BotService
        from framework.core.types import LLMResponse
        from framework.pipeline.adapters import InputAdapter, OutputAdapter, OutputMessage

        class _MockInputAdapter(InputAdapter):
            @property
            def name(self): return "mock_input"
            async def start(self): pass
            async def stop(self): pass
            def receive(self):
                async def _gen():
                    if False:
                        yield None
                return _gen()

        class _MockOutputAdapter(OutputAdapter):
            @property
            def name(self): return "mock_output"
            @property
            def supports_streaming(self): return False
            async def send(self, message: OutputMessage, session_id: str): pass
            async def send_delta(self, delta: str, session_id: str, metadata=None): pass
            async def flush_deltas(self, session_id: str): pass

        class _MockProvider:
            async def chat(self, messages=None, **kwargs):
                return LLMResponse(content="ok")
            def get_default_model(self): return "mock"

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "bot_config.yml").write_text(
            """
llm:
  model: mock
  api_key: key
agent:
  system_prompt: "test"
multi_agent:
  enabled: true
  parent_agent_name: main
  subagent_sync:
    enabled: true
    name: helper
  subagent_async:
    enabled: true
    name: helper
tools:
  file_tools:
    enabled: false
  shell_tools:
    enabled: false
mcp:
  servers: {}
""",
            encoding="utf-8",
        )

        with patch.object(BotService, "_create_provider", return_value=_MockProvider()):
            service = BotService(
                config_dir=config_dir,
                input_adapter=_MockInputAdapter(),
                output_adapter=_MockOutputAdapter(),
                emitter_factory=lambda sid: None,
                mode="pool",
            )
            await service.initialize()

            assert service.agent_pool is not None
            resident_names = [d.address.name for d in service.agent_pool.list_agents()]
            assert "main" in resident_names
            assert "helper" in resident_names

            await service.stop()


# Run async tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
