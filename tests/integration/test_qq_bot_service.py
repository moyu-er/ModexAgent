"""Integration tests for bot_service.py components.

验证端到端流程：
- QQBotService 组件初始化
- QQBotEmitter 与 QQOutputAdapter 的集成
- 流式/非流式模式切换
- 推理内容处理流程
"""

from enum import Enum
from typing import Any, TypeVar

import pytest

pytestmark = pytest.mark.integration
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add framework path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.core.constants import StopReason
from framework.core.emitter import AgentResult, ContentEmitter, EmitterConfig
from framework.core.events import AgentEvent
from framework.core.session_id import SessionInfo

E = TypeVar('E', bound=AgentEvent)


class _BufferingEmitter(ContentEmitter[E]):
    """Minimal test emitter that captures output for assertions."""

    def __init__(self, config: EmitterConfig | None = None):
        super().__init__(config)
        self._buffer = ""
        self._reasoning_buffer = ""
        self._result: AgentResult | None = None
        self._events: list[tuple[E, Any]] = []

    async def emit(self, event: E, data: Any = None) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        if self.config.is_enabled(event_name):
            self._events.append((event, data))
        await super().emit(event, data)

    async def _on_event(self, event: E, data: Any = None) -> None:
        event_name = event.value if isinstance(event, Enum) else str(event)
        if event_name == "model_reasoning":
            if isinstance(data, str):
                self._reasoning_buffer += data

    async def emit_delta(self, delta: str) -> None:
        self._buffer += delta

    async def emit_content(self, full_content: str) -> None:
        self._buffer += full_content

    async def emit_complete(self, result: AgentResult) -> None:
        self._result = result

    async def emit_error(self, error: str) -> None:
        self._result = AgentResult(error=error, stop_reason=StopReason.ERROR)

    def get_content(self) -> str:
        return self._buffer

    def get_reasoning(self) -> str:
        return self._reasoning_buffer

    def get_events(self, event_type: E | None = None) -> list[tuple[E, Any]]:
        if event_type is not None:
            return [(e, d) for e, d in self._events if e == event_type]
        return list(self._events)

    def get_events_by_name(self, name: str) -> list[tuple[E, Any]]:
        result = []
        for e, d in self._events:
            if isinstance(e, Enum) and e.name == name or isinstance(e, str) and e == name:
                result.append((e, d))
        return result


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
        from framework import ReActEvent, StreamingAwareEmitter

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
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'examples' / 'bot_project'))

        try:
            from bot.adapters.qq import QQBotEmitter

            from framework.agents.react import ReActEvent
            from framework.core.emitter import AgentResult
            from framework.core.types import ToolCall

            # Create mock adapter
            mock_adapter = MagicMock()
            mock_adapter.streaming_mode = StreamingMode.NONE
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
            session=SessionInfo.from_str("test.agent"),
        )

        # Test streaming mode (emitter wants streaming)
        class StreamingEmitter(_BufferingEmitter[ReActEvent]):
            def wants_streaming(self):
                return True

        emitter = StreamingEmitter()
        await agent.run(context, emitter)
        assert provider.chat_stream_called is True
        assert provider.chat_called is False

        # Reset
        provider.chat_stream_called = False
        provider.chat_called = False

        # Test non-streaming mode (emitter doesn't want streaming)
        emitter2 = _BufferingEmitter[ReActEvent]()
        await agent.run(context, emitter2)
        assert provider.chat_stream_called is False
        assert provider.chat_called is True

    def test_output_adapter_send_delta_interface(self):
        """Test that OutputAdapter has the send_delta interface."""
        from framework.pipeline.adapters import OutputAdapter

        # Check that send_delta method exists
        assert hasattr(OutputAdapter, "send_delta")

        assert hasattr(OutputAdapter, "streaming_mode")

    @pytest.mark.asyncio
    async def test_end_to_end_event_flow(self):
        """Test complete event flow from Agent to QQ Output."""
        from framework.agents.react import ReActAgent, ReActEvent
        from framework.core.agent import AgentContext
        from framework.core.emitter import StreamingAwareEmitter

        # Track events
        events_received = []

        class MockAdapter:
            def __init__(self):
                self.streaming_mode = StreamingMode.NONE

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
            session=SessionInfo.from_str("test.agent"),
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
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'examples' / 'bot_project'))

        try:
            from bot.service.qq_service import QQBotService

            # Verify current IOC-based service construction surface.
            assert hasattr(QQBotService, "initialize")
            assert hasattr(QQBotService, "start")

        except ImportError as e:
            pytest.skip(f"QQBotService not importable: {e}")

    @pytest.mark.asyncio
    async def test_qq_bot_skills_use_compact_prompt(self):
        """Regression test: QQ Bot skills must produce a compact prompt table,
        not inline full skill content, to avoid exceeding LLM context limits.
        """
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / 'examples' / 'bot_project'))

        from pathlib import Path

        from framework.core.skills import (
            FileSkillSource,
            DefaultSkillBuilder,
            ResolutionContext,
            SkillManager,
        )

        skills_dir = Path(__file__).parent.parent.parent / 'examples' / 'bot_project' / 'skills' / 'main' / 'main'
        if not skills_dir.exists():
            pytest.skip("bot_project/skills/main directory not found")

        source = FileSkillSource(
            directories=[skills_dir],
            cache=True,
            layout="directory",
            skill_filename="SKILL.md",
        )
        sm = SkillManager(source=source, builder=DefaultSkillBuilder())

        class FakeTM:
            def has_tool(self, name: str) -> bool:
                return name == "read_file"

        ctx = ResolutionContext(tool_manager=FakeTM())
        prompt = await sm.build_prompt(ctx)

        # Should be a compact table, not inlined content
        assert "<available_skills>" in prompt
        # Must NOT contain full skill body text that would bloat the context
        assert prompt.count("<skill name=") > 0  # at least one skill listed
        # Each skill should appear as a single table row, not as multi-line content
        assert prompt.count("<available_skills>") == 1


    @pytest.mark.asyncio
    async def test_bot_service_pool_mode_bridge_routing(self, tmp_path):
        """BotService 通过 BrokerBridgeService 正确路由输入/输出。"""
        import sys
        from pathlib import Path

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot.service.core import BotService

        from framework.core.types import InputMessage, LLMResponse
        from framework.adapters.platform import StreamingMode
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
            def streaming_mode(self):
                return StreamingMode.NONE

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

multi_agent:
  default_pool: main
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
        pools_dir = config_dir / "pools"
        pools_dir.mkdir()
        (pools_dir / "main.yml").write_text(
            """
llm:
  model: mock-model
  api_key: test-key
  base_url: ""
  temperature: 0.7
  max_tokens: 100

agents:
  - name: main
    role: main
    system_prompt: "You are a test agent."
    max_steps: 1

memory:
  short_term:
    max_messages: 10
    max_tokens: 100
    budget_ratio: 0.5
""",
            encoding="utf-8",
        )

        input_adapter = _MockInputAdapter()
        output_adapter = _MockOutputAdapter()

        def _emitter_factory(session_id: str):
            from framework.agents.react import ReActEvent
            return _BufferingEmitter[ReActEvent]()

        with patch("bot.service.pool_builder.create_llm_provider", return_value=_MockProvider()), patch(
            "bot.service.pool_builder._load_agent_mcp_tools", return_value=([], None)
        ):
            service = BotService(
                config_dir=config_dir,
                input_adapter=input_adapter,
                output_adapter=output_adapter,
                emitter_factory=_emitter_factory,
            )
            await service.initialize()

            # 启动 pool 模式（只启动 bridge，不阻塞）
            start_task = asyncio.create_task(service.start())
            await asyncio.sleep(0.1)

            # 注入一条消息
            await input_adapter.inject(InputMessage(content="ping", session=SessionInfo.from_str("s1", default_agent_name="main")))

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
    async def test_bot_service_pool_registers_subagent_residents(self, tmp_path):
        """pool 模式下 initialize 后主 Agent 应为常驻代理，且注册了 send_to_agent 工具。"""
        import sys
        from pathlib import Path

        qq_project = Path(__file__).parent.parent.parent / "examples" / "bot_project"
        if str(qq_project) not in sys.path:
            sys.path.insert(0, str(qq_project))

        from bot.service.core import BotService

        from framework.core.types import LLMResponse
        from framework.adapters.platform import StreamingMode
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
multi_agent:
  enabled: true
  default_pool: main
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
        pools_dir = config_dir / "pools"
        pools_dir.mkdir()
        (pools_dir / "main.yml").write_text(
            """
llm:
  model: mock
  api_key: key
agents:
  - name: main
    role: main
    system_prompt: "test"
  - name: helper
    role: subagent
    system_prompt: "helper"
memory:
  short_term:
    max_messages: 10
    max_tokens: 100
    budget_ratio: 0.5
""",
            encoding="utf-8",
        )

        with patch("bot.service.pool_builder.create_llm_provider", return_value=_MockProvider()), patch(
            "bot.service.pool_builder._load_agent_mcp_tools", return_value=([], None)
        ):
            service = BotService(
                config_dir=config_dir,
                input_adapter=_MockInputAdapter(),
                output_adapter=_MockOutputAdapter(),
                emitter_factory=lambda sid: None,
            )
            await service.initialize()

            # Pool should exist and have the main agent as resident
            pool = service._pools["main"].pool
            assert pool is not None
            resident_names = [d.address.name for d in pool.list_agents()]
            assert "main" in resident_names

            # Main agent should have send_to_agent tool (for communicating with subagents)
            main_agent = pool.get("main")
            assert main_agent is not None
            assert main_agent.pipeline is not None
            assert "send_to_agent" in main_agent.pipeline.tool_manager.list_tools()

            await service.stop()


# Run async tests
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
